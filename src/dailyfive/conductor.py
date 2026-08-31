"""Conductor — fires, waits, retries, mirrors. No language model in here either.

This is the boring half, and it is the half that decides whether the studio is
still running in six months. Three rules it exists to enforce:

**Poll as well as listen.** Suno abandons a callback after three consecutive
delivery failures. A pipeline that trusts webhooks alone loses the run silently,
with no error raised anywhere. Every job is polled on a timer regardless of what
arrives on the webhook.

**Never pay twice.** The job row, with its idempotency key, is written and
committed *before* the request is sent. A process that dies between POST and
commit is resumed by matching on that key rather than submitting again.

**Mirror the moment bytes exist.** Suno retains generated files for 15 days and
its download URLs are short-lived. Once a file is in Spaces, every upstream
expiry stops being a problem this system has.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .errors import BudgetExceeded, ProviderError
from .http import download
from .models import (SUNO_FAILURE_STATUSES, SUNO_STATUS_MAP, TERMINAL_JOB_STATES,
                     Brief, Clip, Job, JobState, Run, SlotType, utcnow)

# await_all is waiting for *generation* to finish, not for mirroring — which is
# a separate phase that runs afterwards. Treating only TERMINAL_JOB_STATES as
# done would mean a perfectly successful job is waited on until the timeout.
GENERATION_DONE = TERMINAL_JOB_STATES | {JobState.SUCCESS}
from .providers.suno import SunoClient

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 20.0
GENERATION_TIMEOUT_S = 15 * 60
WAV_TIMEOUT_S = 10 * 60
MAX_ATTEMPTS = 3


class Conductor:
    def __init__(self, run_id: int, *, client: SunoClient | None = None,
                 work_dir: Path | None = None):
        self.run_id = run_id
        self.client = client or SunoClient()
        self.work_dir = Path(work_dir or settings().work_dir) / str(run_id)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ── submission ───────────────────────────────────────────────────────────
    def submit_all(self, *, credit_cap: int | None = None) -> list[int]:
        """Submit every queued job for this run. Returns job ids in flight."""
        cap = credit_cap if credit_cap is not None else settings().daily_credit_cap
        with session_scope() as s:
            jobs = s.execute(
                select(Job).where(Job.run_id == self.run_id,
                                  Job.state.in_([JobState.QUEUED, JobState.SUBMITTED]))
                .order_by(Job.id)).scalars().all()
            job_ids = [j.id for j in jobs]

        in_flight: list[int] = []
        for job_id in job_ids:
            try:
                if self._submit_one(job_id, cap):
                    in_flight.append(job_id)
            except BudgetExceeded:
                log.error("credit cap reached — abandoning remaining submissions")
                self._abandon_remaining("daily credit cap reached")
                break
            except ProviderError as exc:
                log.error("job %d could not be submitted: %s", job_id, exc)
        return in_flight

    def _submit_one(self, job_id: int, cap: int) -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.state in TERMINAL_JOB_STATES:
                return False
            # Already submitted and committed — resume rather than re-send.
            if job.task_id:
                log.info("job %d already has taskId %s, resuming", job_id, job.task_id)
                job.state = JobState.PENDING
                return True
            if job.attempts >= MAX_ATTEMPTS:
                job.state = JobState.ABANDONED
                job.last_error = f"exhausted {MAX_ATTEMPTS} submission attempts"
                return False
            payload = dict(job.payload)
            job.attempts += 1

        self._check_budget(cap)

        try:
            task_id = self.client.generate(payload)
        except ProviderError as exc:
            with session_scope() as s:
                job = s.get(Job, job_id)
                job.last_error = str(exc)[:1000]
                job.failure_kind = "submit"
                if not exc.retryable or job.attempts >= MAX_ATTEMPTS:
                    job.state = JobState.FAILED
                    job.finished_at = utcnow()
            raise

        with session_scope() as s:
            job = s.get(Job, job_id)
            job.task_id = task_id
            job.state = JobState.SUBMITTED
            job.submitted_at = utcnow()
            job.last_seen_at = utcnow()
        log.info("job %d submitted as task %s", job_id, task_id)
        return True

    def _check_budget(self, cap: int) -> None:
        with session_scope() as s:
            run = s.get(Run, self.run_id)
            start = run.credits_start if run else None
        if start is None:
            return
        try:
            now = self.client.credits()
        except ProviderError:
            return  # a credit check that fails is not a reason to stop the run
        if start - now >= cap:
            raise BudgetExceeded(f"spent {start - now} credits, cap is {cap}")

    def _abandon_remaining(self, why: str) -> None:
        with session_scope() as s:
            for job in s.execute(
                select(Job).where(Job.run_id == self.run_id,
                                  Job.state == JobState.QUEUED)).scalars():
                job.state = JobState.ABANDONED
                job.last_error = why
                job.finished_at = utcnow()

    # ── waiting ──────────────────────────────────────────────────────────────
    def await_all(self, *, timeout_s: float = GENERATION_TIMEOUT_S,
                  poll_interval_s: float = POLL_INTERVAL_S,
                  sleep=time.sleep) -> dict[str, int]:
        """Block until every job reaches a terminal state or the timeout.

        Callbacks land in the database via the web receiver; this loop reads
        that state and polls anything that has not moved. Both paths converge
        on :meth:`ingest_record` so a webhook and a poll cannot disagree.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pending = self._pending_jobs()
            if not pending:
                break
            for job_id, task_id, last_seen in pending:
                if not task_id:
                    continue
                # Poll anything we have not heard about within the interval.
                if last_seen and _age_seconds(last_seen) < poll_interval_s:
                    continue
                try:
                    record = self.client.record_info(task_id)
                    self.ingest_record(job_id, record, source="poll")
                except ProviderError as exc:
                    log.warning("poll failed for job %d: %s", job_id, exc)
            if self._pending_jobs():
                sleep(poll_interval_s)

        stale = self._pending_jobs()
        for job_id, task_id, _ in stale:
            log.error("job %d (task %s) never completed — abandoning", job_id, task_id)
            with session_scope() as s:
                job = s.get(Job, job_id)
                job.state = JobState.ABANDONED
                job.last_error = f"no terminal state within {timeout_s:.0f}s"
                job.finished_at = utcnow()

        return self.tally()

    def _pending_jobs(self) -> list[tuple[int, str | None, datetime | None]]:
        with session_scope() as s:
            rows = s.execute(
                select(Job.id, Job.task_id, Job.last_seen_at)
                .where(Job.run_id == self.run_id,
                       Job.state.notin_(list(GENERATION_DONE)))).all()
        return [(r.id, r.task_id, r.last_seen_at) for r in rows]

    def tally(self) -> dict[str, int]:
        with session_scope() as s:
            jobs = s.execute(select(Job).where(Job.run_id == self.run_id)).scalars().all()
            clips = s.execute(select(Clip).where(Clip.run_id == self.run_id)).scalars().all()
        out: dict[str, int] = {"clips": len(clips)}
        for j in jobs:
            key = j.state.value if isinstance(j.state, JobState) else str(j.state)
            out[key] = out.get(key, 0) + 1
        return out

    # ── ingestion (one path for both webhooks and polls) ─────────────────────
    def ingest_record(self, job_id: int, record: dict, *, source: str = "poll") -> JobState:
        """Fold one status report into the job, whatever delivered it."""
        status = record.get("status") or record.get("successFlag")
        data = record.get("response") or record.get("data") or {}
        clips = data.get("sunoData") or data.get("data") or []

        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise ValueError(f"no job {job_id}")
            job.last_seen_at = utcnow()
            job.polls += 1 if source == "poll" else 0
            job.callbacks_seen += 1 if source == "callback" else 0

            if status in SUNO_FAILURE_STATUSES:
                kind, retryable = SUNO_FAILURE_STATUSES[status]
                job.failure_kind = kind
                job.last_error = (record.get("errorMessage")
                                  or f"suno reported {status}")[:1000]
                # A moderation refusal never succeeds on retry — the lyric has
                # to change, which is the Clearance agent's problem, not ours.
                job.state = JobState.FAILED
                job.finished_at = utcnow()
                log.error("job %d failed: %s (%s, retryable=%s)",
                          job_id, status, kind, retryable)
                return job.state

            mapped = SUNO_STATUS_MAP.get(status or "")
            if mapped is None:
                job.last_error = f"unrecognised suno status {status!r}"
                log.warning("job %d: %s", job_id, job.last_error)
                return job.state

            if _rank(mapped) > _rank(job.state):
                job.state = mapped
            brief_id = job.brief_id
            run_id = job.run_id

        if clips:
            self._record_clips(job_id, brief_id, run_id, clips)

        with session_scope() as s:
            job = s.get(Job, job_id)
            if job.state == JobState.SUCCESS:
                job.finished_at = utcnow()
            return job.state

    def _record_clips(self, job_id: int, brief_id: int, run_id: int,
                      raw_clips: list[dict]) -> None:
        """Insert clip rows, and refresh ones already seen.

        FIRST_SUCCESS then SUCCESS means the first clip arrives twice, so the
        unique constraint on (job_id, audio_id) stops a duplicate row. But the
        two deliveries are not identical: the earlier one often has no duration
        and no final audio URL, because the render was not finished. Skipping
        the repeat outright therefore froze whatever was missing the first time
        — a shipped song reached its meta.json with a null duration.

        So a repeat updates the fields that have since become available, and
        never overwrites something real with a None.
        """
        from .providers.suno import SunoClip

        with session_scope() as s:
            job = s.get(Job, job_id)
            brief = s.get(Brief, brief_id)
            payload = dict(job.payload)
            existing = {c.audio_id for c in job.clips}

            by_audio = {c.audio_id: c for c in job.clips}

            for idx, raw in enumerate(raw_clips):
                parsed = SunoClip.from_payload(raw)
                if not parsed.audio_id:
                    continue
                if parsed.audio_id in existing:
                    row = by_audio.get(parsed.audio_id)
                    if row is not None and _refresh(row, parsed):
                        log.info("job %d: refreshed clip %s with late-arriving data",
                                 job_id, parsed.audio_id)
                    continue
                existing.add(parsed.audio_id)
                s.add(Clip(
                    run_id=run_id, job_id=job_id, brief_id=brief_id,
                    audio_id=parsed.audio_id, variant=idx,
                    slot_type=brief.slot_type,
                    theme=brief.theme,
                    genre_family=brief.genre_family, genre=brief.genre,
                    language=brief.language,
                    style_string=payload.get("style"),
                    negative_tags=payload.get("negativeTags"),
                    persona_id=payload.get("personaId"), model=payload.get("model"),
                    vocal_gender=payload.get("vocalGender"),
                    style_weight=payload.get("styleWeight"),
                    weirdness=payload.get("weirdnessConstraint"),
                    audio_weight=payload.get("audioWeight"),
                    bpm_target=brief.bpm, musical_key=brief.musical_key,
                    song_form=brief.song_form, lyric_hash=brief.lyric_hash,
                    title=parsed.title or brief.title, tags=parsed.tags,
                    duration_s=parsed.duration,
                    source_audio_url=parsed.audio_url or parsed.stream_audio_url,
                    source_image_url=parsed.image_url,
                ))
                log.info("job %d: recorded clip %s (%s)", job_id, parsed.audio_id,
                         parsed.title or "untitled")

    # ── mirroring ────────────────────────────────────────────────────────────
    def mirror_all(self) -> list[int]:
        """Download every clip that has a URL and no local file yet."""
        with session_scope() as s:
            rows = s.execute(
                select(Clip.id, Clip.audio_id, Clip.source_audio_url, Clip.local_path)
                .where(Clip.run_id == self.run_id)).all()

        mirrored: list[int] = []
        for clip_id, audio_id, url, local in rows:
            if local and Path(local).is_file():
                mirrored.append(clip_id)
                continue
            if not url:
                log.warning("clip %s has no audio URL yet", audio_id)
                continue
            dest = self.work_dir / f"{audio_id}.mp3"
            try:
                size = download(url, dest, provider="suno-audio")
            except ProviderError as exc:
                log.error("clip %s download failed: %s", audio_id, exc)
                continue
            with session_scope() as s:
                clip = s.get(Clip, clip_id)
                clip.local_path = str(dest)
            log.info("mirrored %s (%.1f MB)", audio_id, size / 1e6)
            mirrored.append(clip_id)

        with session_scope() as s:
            for job in s.execute(
                select(Job).where(Job.run_id == self.run_id,
                                  Job.state == JobState.SUCCESS)).scalars():
                if job.clips and all(c.local_path for c in job.clips):
                    job.state = JobState.MIRRORED
        return mirrored

    # ── WAV conversion, for the picks only ───────────────────────────────────
    def request_wav(self, clip_id: int) -> str | None:
        with session_scope() as s:
            clip = s.get(Clip, clip_id)
            if clip.wav_task_id:
                return clip.wav_task_id
            task_id = clip.job.task_id
            audio_id = clip.audio_id
        if not task_id:
            return None
        try:
            wav_task = self.client.wav_generate(task_id, audio_id)
        except ProviderError as exc:
            # 409 means a conversion already exists for this clip, which is a
            # success from our side — the poll below will find it.
            if exc.code == 409:
                log.info("wav already requested for %s", audio_id)
                return None
            log.error("wav request failed for %s: %s", audio_id, exc)
            return None
        with session_scope() as s:
            s.get(Clip, clip_id).wav_task_id = wav_task
        return wav_task

    def await_wav(self, clip_id: int, *, timeout_s: float = WAV_TIMEOUT_S,
                  poll_interval_s: float = 15.0, sleep=time.sleep) -> str | None:
        with session_scope() as s:
            clip = s.get(Clip, clip_id)
            wav_task, existing = clip.wav_task_id, clip.wav_url
        if existing:
            return existing
        if not wav_task:
            return None

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                rec = self.client.wav_record_info(wav_task)
            except ProviderError as exc:
                log.warning("wav poll failed: %s", exc)
                sleep(poll_interval_s)
                continue

            flag = rec.get("successFlag")
            url = (rec.get("response") or {}).get("audioWavUrl")
            if flag == "SUCCESS" and url:
                with session_scope() as s:
                    s.get(Clip, clip_id).wav_url = url
                return url
            if flag in ("CREATE_TASK_FAILED", "GENERATE_WAV_FAILED", "CALLBACK_EXCEPTION"):
                log.error("wav conversion failed for clip %d: %s", clip_id, flag)
                return None
            sleep(poll_interval_s)

        log.error("wav conversion for clip %d timed out", clip_id)
        return None


def _age_seconds(when: datetime) -> float:
    """Seconds since ``when``, tolerating a naive timestamp.

    SQLite has no timezone type, so a column declared ``DateTime(timezone=True)``
    reads back naive there and aware on Postgres. Comparing the two raises, and
    it would only ever raise on the SQLite path — which is the one people start
    on. Naive values are read as UTC, which is what was written.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (utcnow() - when).total_seconds()


def _refresh(row: "Clip", parsed) -> bool:
    """Fill in anything the earlier delivery did not have. Never clears a value."""
    changed = False
    for attr, value in (("duration_s", parsed.duration),
                        ("source_audio_url", parsed.audio_url),
                        ("source_image_url", parsed.image_url),
                        ("tags", parsed.tags),
                        ("title", parsed.title)):
        if value in (None, "") or getattr(row, attr) == value:
            continue
        # A real audio URL supersedes the stream URL recorded at FIRST_SUCCESS.
        if attr == "source_audio_url" and row.source_audio_url and not parsed.audio_url:
            continue
        setattr(row, attr, value)
        changed = True
    return changed


_ORDER = [JobState.QUEUED, JobState.SUBMITTED, JobState.PENDING, JobState.TEXT_SUCCESS,
          JobState.FIRST_SUCCESS, JobState.SUCCESS, JobState.MIRRORED]


def _rank(state: JobState) -> int:
    """Progress ordering, so a late-arriving PENDING cannot undo a SUCCESS."""
    try:
        return _ORDER.index(state)
    except ValueError:
        return 99
