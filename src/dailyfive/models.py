"""The ten tables.

Two consumers with different needs share this schema. The Conductor needs
crash-safe resume: every externally-visible action is recorded before it is
taken, so a process that dies mid-run can be restarted without double-spending.
The Archivist needs the learning record: one ``Clip`` row per candidate,
written whether or not it shipped, because the rejections are what teach.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey,
                        Integer, LargeBinary, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunPhase(str, enum.Enum):
    """Where a run got to. Restarting resumes from the recorded phase."""
    CREATED = "created"
    SENSED = "sensed"
    BRIEFED = "briefed"
    WRITTEN = "written"
    SUBMITTED = "submitted"
    RENDERED = "rendered"
    JUDGED = "judged"
    SHIPPED = "shipped"
    FAILED = "failed"


class JobState(str, enum.Enum):
    """Our view of a Suno task.

    The middle five mirror Suno's own ``status`` values; the outer ones are
    ours. MIRRORED is the only state that means the bytes are safe — everything
    before it depends on a URL that expires.
    """
    QUEUED = "queued"
    SUBMITTED = "submitted"
    PENDING = "pending"
    TEXT_SUCCESS = "text_success"
    FIRST_SUCCESS = "first_success"
    SUCCESS = "success"
    MIRRORED = "mirrored"
    FAILED = "failed"
    ABANDONED = "abandoned"


TERMINAL_JOB_STATES = {JobState.MIRRORED, JobState.FAILED, JobState.ABANDONED}

# Suno status -> our state. Anything unmapped is treated as a failure and the
# raw value is kept in Job.last_error so a new status value shows up in the log
# rather than silently stalling the run.
SUNO_STATUS_MAP = {
    "PENDING": JobState.PENDING,
    "TEXT_SUCCESS": JobState.TEXT_SUCCESS,
    "FIRST_SUCCESS": JobState.FIRST_SUCCESS,
    "SUCCESS": JobState.SUCCESS,
}

SUNO_FAILURE_STATUSES = {
    "CREATE_TASK_FAILED": ("transient", True),
    "GENERATE_AUDIO_FAILED": ("render", True),
    "CALLBACK_EXCEPTION": ("callback", True),
    "SENSITIVE_WORD_ERROR": ("moderation", False),
}


class SlotType(str, enum.Enum):
    FULL = "full"
    SHORT = "short"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    phase: Mapped[RunPhase] = mapped_column(Enum(RunPhase), default=RunPhase.CREATED)
    codex_version: Mapped[int | None] = mapped_column(Integer)
    credits_start: Mapped[int | None] = mapped_column(Integer)
    credits_end: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    briefs: Mapped[list["Brief"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    clips: Mapped[list["Clip"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    @property
    def credits_spent(self) -> int | None:
        if self.credits_start is None or self.credits_end is None:
            return None
        return max(0, self.credits_start - self.credits_end)


class Signal(Base):
    """One theme the Scout surfaced, with the evidence that produced it."""
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    theme: Mapped[str] = mapped_column(String(200))
    sentiment: Mapped[str] = mapped_column(String(80))
    sources: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[str] = mapped_column(Text, default="")
    lead: Mapped[str] = mapped_column(String(20), default="moderate")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodexVersion(Base):
    """The Style Codex and persona cast, versioned.

    Never updated in place — the Director writes a new row with a diff and a
    rationale, so a regression can be traced to the edit that caused it.
    """
    __tablename__ = "codex_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    body: Mapped[dict] = mapped_column(JSON)
    personas: Mapped[list] = mapped_column(JSON, default=list)
    diff: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    slot_type: Mapped[SlotType] = mapped_column(Enum(SlotType))
    idx: Mapped[int] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(String(200))
    theme: Mapped[str] = mapped_column(Text)
    persona_id: Mapped[str | None] = mapped_column(String(120))
    persona_name: Mapped[str | None] = mapped_column(String(120))

    # Musical spec from the Director — the checkable half of the brief.
    bpm: Mapped[int | None] = mapped_column(Integer)
    musical_key: Mapped[str | None] = mapped_column(String(40))
    song_form: Mapped[str | None] = mapped_column(Text)
    instrumentation: Mapped[str | None] = mapped_column(Text)
    vocal_gender: Mapped[str | None] = mapped_column(String(4))
    style_string: Mapped[str | None] = mapped_column(Text)
    negative_tags: Mapped[str | None] = mapped_column(Text)

    # The one controlled term in a brief otherwise made of prose. String and
    # not Enum, though RunPhase, JobState and SlotType next door are all native
    # Enums: those three are closed sets that will not grow, while a genre
    # vocabulary is expected to. A Postgres ENUM can never drop a value, and
    # ALTER TYPE ADD VALUE historically cannot run inside a transaction block —
    # which is what Alembic wraps every migration in — so Enum would turn
    # "add a genre" from a one-line constant edit into a migration with a
    # caveat. The closed set is enforced in genres.normalise() at write time.
    genre_family: Mapped[str | None] = mapped_column(String(24))
    genre: Mapped[str | None] = mapped_column(String(40))

    diversity_vector: Mapped[dict] = mapped_column(JSON, default=dict)
    lyrics: Mapped[str | None] = mapped_column(Text)
    lyric_hash: Mapped[str | None] = mapped_column(String(64))
    clearance: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dropped_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped["Run"] = relationship(back_populates="briefs")
    jobs: Mapped[list["Job"]] = relationship(back_populates="brief", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("run_id", "slot_type", "idx", name="uq_brief_slot"),)


class Job(Base):
    """One Suno generation task.

    ``idempotency_key`` is what makes a crashed run safe to restart: the
    Conductor looks for an existing job with the same key before submitting,
    so a process that died between POST and commit does not pay twice.
    """
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id", ondelete="CASCADE"), index=True)

    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.QUEUED, index=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    callbacks_seen: Mapped[int] = mapped_column(Integer, default=0)
    polls: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_kind: Mapped[str | None] = mapped_column(String(40))

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped["Run"] = relationship(back_populates="jobs")
    brief: Mapped["Brief"] = relationship(back_populates="jobs")
    clips: Mapped[list["Clip"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Clip(Base):
    """The learning record. One row per candidate, shipped or not.

    Everything the Archivist needs to answer "which style strings actually
    work" lives here, denormalised on purpose — a query that has to join five
    tables to compare two prompts does not get written.

    ``genre_family`` and ``genre`` are the genre that was ASKED FOR, copied
    from the brief. Nothing in this pipeline observes what actually came back:
    Suno echoes the submitted style string into ``tags`` unchanged, and QC is
    ffmpeg-only — loudness, true peak, silence, duration, nothing that
    identifies a genre. So there is no ``genre_observed`` column, because an
    always-null one would advertise a capability that does not exist and invite
    a join that silently returns nothing. The day a classifier exists, the
    observed value goes in a new column beside these two. Until then the free
    proxy for drift is that the two clips of a pair share a brief, a prompt and
    a genre: persistent high within-pair score variance for a family means the
    prompt is not controlling the outcome.
    """
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id", ondelete="CASCADE"), index=True)

    audio_id: Mapped[str] = mapped_column(String(120), index=True)
    variant: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1 of the pair

    # What produced it — denormalised so a single SELECT answers "what worked".
    slot_type: Mapped[SlotType] = mapped_column(Enum(SlotType))
    theme: Mapped[str | None] = mapped_column(Text)
    genre_family: Mapped[str | None] = mapped_column(String(24))
    genre: Mapped[str | None] = mapped_column(String(40))
    style_string: Mapped[str | None] = mapped_column(Text)
    negative_tags: Mapped[str | None] = mapped_column(Text)
    persona_id: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(40))
    vocal_gender: Mapped[str | None] = mapped_column(String(4))
    style_weight: Mapped[float | None] = mapped_column(Float)
    weirdness: Mapped[float | None] = mapped_column(Float)
    audio_weight: Mapped[float | None] = mapped_column(Float)
    bpm_target: Mapped[int | None] = mapped_column(Integer)
    musical_key: Mapped[str | None] = mapped_column(String(40))
    song_form: Mapped[str | None] = mapped_column(Text)
    lyric_hash: Mapped[str | None] = mapped_column(String(64))

    # What came back.
    title: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    source_audio_url: Mapped[str | None] = mapped_column(Text)
    source_image_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    spaces_key: Mapped[str | None] = mapped_column(Text)
    wav_task_id: Mapped[str | None] = mapped_column(String(120))
    wav_url: Mapped[str | None] = mapped_column(Text)

    # What measurement said.
    qc: Mapped[dict] = mapped_column(JSON, default=dict)
    qc_verdict: Mapped[str | None] = mapped_column(String(20), index=True)
    qc_reason: Mapped[str | None] = mapped_column(Text)

    # What the Producer said.
    score_hook: Mapped[float | None] = mapped_column(Float)
    score_mix: Mapped[float | None] = mapped_column(Float)
    score_trend: Mapped[float | None] = mapped_column(Float)
    score_total: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    shipped: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped["Run"] = relationship(back_populates="clips")
    job: Mapped["Job"] = relationship(back_populates="clips")
    outcome: Mapped["Outcome | None"] = relationship(back_populates="clip", uselist=False,
                                                     cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("job_id", "audio_id", name="uq_clip_audio"),)


class AgentCall(Base):
    """One brain call, recorded whether it succeeded or not.

    This is what makes the roster visible rather than notional: which role ran,
    which brain answered, how long it took, how much it moved, and what broke.
    Without it "twelve agents" is a claim in a document.
    """
    __tablename__ = "agent_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    chars_in: Mapped[int] = mapped_column(Integer, default=0)
    chars_out: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StoredFile(Base):
    """A delivered file, kept in the database with an expiry.

    Audio lives here rather than in object storage because that is how this
    deployment was asked to work: one place to look, one retention rule, one
    thing to back up. It costs more per gigabyte than Spaces and it makes
    backups bigger, which is the trade being made deliberately — at 30 days and
    five songs a day it is roughly 8 GB of audio, which a 30 GB cluster carries
    with room to spare.

    ``expires_at`` is set on write, not computed on read, so the retention
    window is a fact about each row rather than a rule someone has to remember
    to apply. Deletion is a job that reads this column and nothing else.
    """
    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(400), unique=True, index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    clip_id: Mapped[int | None] = mapped_column(
        ForeignKey("clips.id", ondelete="SET NULL"), index=True)

    kind: Mapped[str] = mapped_column(String(24), index=True)   # wav | mp3 | cover | video | text
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Decision(Base):
    """The Producer's written reasoning for one run. Kept separate from Clip
    because it is prose about the whole field, not a per-clip fact."""
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    picks: Mapped[list] = mapped_column(JSON, default=list)
    rejections: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outcome(Base):
    """Your rating, and anything reality adds later.

    This is the only field the pipeline cannot fill in for itself. Until it is
    populated, the loop optimises for the Producer's opinion.
    """
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"),
                                         unique=True, index=True)
    rating: Mapped[int | None] = mapped_column(Integer)  # 1–10, from the day page
    note: Mapped[str | None] = mapped_column(Text)
    plays: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    clip: Mapped["Clip"] = relationship(back_populates="outcome")


class Publication(Base):
    """One upload of one song to one platform, and what the platform did with it.

    Separate from ``Outcome`` rather than more columns on it, because they
    answer different questions and arrive at different times. An Outcome is one
    row per clip and holds a judgement. This is one row per clip PER PLATFORM
    and holds a measurement — the same song on TikTok and on YouTube is two
    numbers, and averaging them into a single ``plays`` column would throw away
    the only comparison in the system that says anything about where this music
    actually lands.

    ``metrics`` keeps whatever the platform returned verbatim beside the four
    counts that are pulled out of it. The counts are what the learning loop
    reads; the blob is what makes it possible to answer a question later that
    nobody thought to add a column for.
    """
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("clip_id", "platform", name="uq_publication"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"),
                                         index=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)   # youtube | tiktok

    # Recorded before the upload finishes, so a process that dies mid-upload
    # leaves evidence rather than a silent gap — the same reason Job carries an
    # idempotency key.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    url: Mapped[str | None] = mapped_column(String(400))
    error: Mapped[str | None] = mapped_column(Text)

    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class OAuthToken(Base):
    """One platform's credentials, kept where they can be rewritten.

    In the database and not in the environment, for a reason specific to these
    two APIs: a refresh token here is not a constant. TikTok issues a NEW
    refresh token on every refresh and expires the old one, so a credential
    held in an env var is single-use — it works once, rotates, and the next
    refresh fails with a token the process could not persist. Anything that has
    to survive its own use has to live somewhere writable.

    That makes this the one table in the schema holding a live secret, which is
    why backup.py excludes its contents. A nightly dump of the catalogue is not
    a thing that should also be a set of working credentials for the accounts
    the catalogue is published to.
    """
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), unique=True, index=True)

    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Whose account this posts to. Not used to authenticate anything — it is
    # here so a log line can say which channel a video went to when there is
    # more than one, and so a swapped credential is visible rather than silent.
    account_id: Mapped[str | None] = mapped_column(String(120))
    scope: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)
