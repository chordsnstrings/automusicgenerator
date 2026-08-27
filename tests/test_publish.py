"""Publishing, and the readback that finally replaces a guess with a measurement.

None of this can be exercised against the real platforms — no OAuth consent has
been completed — so what is tested here is the shape: that a row exists before
the upload does, that a duplicate post is refused rather than retried, that one
platform failing does not cost the other its numbers, and that a view count
reaches the codex as a rank rather than as a raw count.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from dailyfive import archivist, publish
from dailyfive.db import session_scope
from dailyfive.errors import ProviderError
from dailyfive.models import Clip, Outcome, Publication, Run, SlotType, utcnow


@pytest.fixture
def a_file(tmp_path):
    f = tmp_path / "short.mp4"
    f.write_bytes(b"not really an mp4, but it is a file")
    return f


@pytest.fixture
def a_clip(brief_factory, run_id):
    from dailyfive.models import Job, JobState
    brief_id = brief_factory(0)
    with session_scope() as s:
        job = Job(run_id=run_id, brief_id=brief_id, state=JobState.SUCCESS,
                  idempotency_key="k0")
        s.add(job)
        s.flush()
        clip = Clip(run_id=run_id, job_id=job.id, brief_id=brief_id,
                    audio_id="a0", slot_type=SlotType.FULL, title="Two Days",
                    shipped=True, score_total=7.0)
        s.add(clip)
        s.flush()
        return clip.id


class FakeBackend:
    """Stands in for youtube/tiktok. Records what it was asked to do."""
    def __init__(self, *, fail=False, stats=None):
        self.fail = fail
        self.stats = stats or {}
        self.uploads = []

    def upload(self, file, *, title, description, tags, privacy):
        if self.fail:
            raise ProviderError("fake", "the platform said no", retryable=False)
        self.uploads.append({"file": file, "title": title, "privacy": privacy})
        return publish.Uploaded(external_id="vid123",
                                url="https://example.test/vid123",
                                extra={"privacy": privacy})

    def statistics(self, ids):
        return {i: self.stats[i] for i in ids if i in self.stats}


def _use(monkeypatch, mapping):
    monkeypatch.setattr(publish, "_backend", lambda p: mapping[p])


# ── uploading ────────────────────────────────────────────────────────────────
def test_a_successful_upload_records_where_it_went(monkeypatch, a_clip, a_file):
    yt = FakeBackend()
    _use(monkeypatch, {"youtube": yt})
    row = publish.publish(clip_id=a_clip, platform="youtube", file=a_file,
                          title="Two Days")
    assert row.status == "live"
    assert row.external_id == "vid123"
    assert yt.uploads[0]["title"] == "Two Days"


def test_a_failed_upload_leaves_evidence_rather_than_a_gap(monkeypatch, a_clip, a_file):
    """A process that dies mid-upload must not look like one that never tried."""
    _use(monkeypatch, {"youtube": FakeBackend(fail=True)})
    with pytest.raises(ProviderError):
        publish.publish(clip_id=a_clip, platform="youtube", file=a_file,
                        title="Two Days")
    with session_scope() as s:
        row = s.execute(select(Publication)).scalar_one()
    assert row.status == "failed"
    assert "said no" in row.error


def test_publishing_the_same_song_twice_is_refused(monkeypatch, a_clip, a_file):
    """An upload has no idempotency key — the platform will take the same file
    twice and give it two ids — so this check is the only thing between a
    timeout and a duplicate posting."""
    yt = FakeBackend()
    _use(monkeypatch, {"youtube": yt})
    publish.publish(clip_id=a_clip, platform="youtube", file=a_file, title="T")
    with pytest.raises(ProviderError, match="already on youtube"):
        publish.publish(clip_id=a_clip, platform="youtube", file=a_file, title="T")
    assert len(yt.uploads) == 1


def test_a_failed_upload_can_be_retried(monkeypatch, a_clip, a_file):
    """Only a live posting blocks a second attempt; a failed one is unfinished
    work, not a duplicate risk."""
    _use(monkeypatch, {"youtube": FakeBackend(fail=True)})
    with pytest.raises(ProviderError):
        publish.publish(clip_id=a_clip, platform="youtube", file=a_file, title="T")
    _use(monkeypatch, {"youtube": FakeBackend()})
    row = publish.publish(clip_id=a_clip, platform="youtube", file=a_file, title="T")
    assert row.status == "live"


def test_the_same_song_reaches_both_platforms_as_two_rows(monkeypatch, a_clip, a_file):
    _use(monkeypatch, {"youtube": FakeBackend(), "tiktok": FakeBackend()})
    for platform in ("youtube", "tiktok"):
        publish.publish(clip_id=a_clip, platform=platform, file=a_file, title="T")
    assert len(publish.for_clip(a_clip)) == 2


def test_a_missing_file_never_reaches_the_platform(monkeypatch, a_clip, tmp_path):
    yt = FakeBackend()
    _use(monkeypatch, {"youtube": yt})
    with pytest.raises(ProviderError, match="nothing to upload"):
        publish.publish(clip_id=a_clip, platform="youtube",
                        file=tmp_path / "gone.mp4", title="T")
    assert not yt.uploads


# ── reading back ─────────────────────────────────────────────────────────────
def _live(clip_id, platform, external_id, **kw):
    with session_scope() as s:
        s.add(Publication(clip_id=clip_id, platform=platform, status="live",
                          external_id=external_id, published_at=utcnow(), **kw))


def test_metrics_land_on_the_row_they_belong_to(monkeypatch, a_clip):
    _live(a_clip, "youtube", "vid1")
    _use(monkeypatch, {"youtube": FakeBackend(stats={
        "vid1": {"views": 4021, "likes": 88, "comments": 3}})})
    out = publish.refresh()
    assert out["updated"] == 1
    with session_scope() as s:
        row = s.execute(select(Publication)).scalar_one()
    assert row.views == 4021 and row.likes == 88
    assert row.metrics_at is not None


def test_one_platform_being_down_does_not_cost_the_other_its_numbers(
        monkeypatch, a_clip):
    _live(a_clip, "youtube", "vid1")
    _live(a_clip, "tiktok", "tik1")

    class Broken(FakeBackend):
        def statistics(self, ids):
            raise ProviderError("tiktok", "out of quota", retryable=True)

    _use(monkeypatch, {
        "youtube": FakeBackend(stats={"vid1": {"views": 10}}),
        "tiktok": Broken(),
    })
    out = publish.refresh()
    assert out["updated"] == 1
    assert out["failed"] and out["failed"][0]["platform"] == "tiktok"


def test_an_unknown_platform_is_refused_by_name():
    with pytest.raises(ProviderError, match="unknown platform"):
        publish._backend("myspace")


# ── what the numbers do to the codex ─────────────────────────────────────────
def _published_clips(counts: list[int], run_id) -> list[int]:
    from dailyfive.models import Brief, Job, JobState
    ids = []
    with session_scope() as s:
        for i, views in enumerate(counts):
            b = Brief(run_id=run_id, slot_type=SlotType.FULL, idx=i,
                      title=f"S{i}", theme="t", lyric_hash=f"h{i}")
            s.add(b)
            s.flush()
            j = Job(run_id=run_id, brief_id=b.id, state=JobState.SUCCESS,
                    idempotency_key=f"key{i}")
            s.add(j)
            s.flush()
            c = Clip(run_id=run_id, job_id=j.id, brief_id=b.id, audio_id=f"a{i}",
                     slot_type=SlotType.FULL, title=f"S{i}", shipped=True,
                     style_string="dark rnb, 808s", score_total=5.0)
            s.add(c)
            s.flush()
            s.add(Publication(clip_id=c.id, platform="youtube", status="live",
                              external_id=f"v{i}", views=views,
                              published_at=utcnow()))
            ids.append(c.id)
    return ids


def test_view_counts_become_a_rank_not_a_raw_number(run_id):
    """A fixed divisor would bake today's channel size into a codex meant to
    outlast it. 400 views is a hit on a small channel and a failure on a big
    one; the only question the loop asks is which of these did better."""
    ids = _published_clips([12, 400, 90_000], run_id)
    scale = archivist.audience_scale()
    assert scale[ids[0]] == 0.0
    assert scale[ids[2]] == 10.0
    assert 0.0 < scale[ids[1]] < 10.0


def test_two_songs_on_the_same_count_score_the_same(run_id):
    """Otherwise the enumeration index leaks into the codex."""
    ids = _published_clips([50, 50, 900], run_id)
    scale = archivist.audience_scale()
    assert scale[ids[0]] == scale[ids[1]]


def test_two_published_songs_are_not_enough_to_rank(run_id):
    """A percentile over two points is a coin toss arriving with the authority
    of a number."""
    _published_clips([10, 5000], run_id)
    assert archivist.audience_scale() == {}


def test_views_are_summed_across_platforms_not_counted_twice(run_id):
    ids = _published_clips([100, 200, 300], run_id)
    with session_scope() as s:
        s.add(Publication(clip_id=ids[0], platform="tiktok", status="live",
                          external_id="t0", views=10_000, published_at=utcnow()))
    scale = archivist.audience_scale()
    assert scale[ids[0]] == 10.0, "the TikTok reach should have moved it to the top"
    assert len(scale) == 3, "one song reaching two audiences is still one song"


def test_the_audience_leads_the_rating_without_silencing_it(run_id):
    ids = _published_clips([10, 100, 1000], run_id)
    with session_scope() as s:
        s.add(Outcome(clip_id=ids[0], rating=10, rated_at=utcnow()))

    with session_scope() as s:
        clip = s.get(Clip, ids[0])
        outcome = s.execute(select(Outcome)
                            .where(Outcome.clip_id == ids[0])).scalar_one()
        blended = archivist._clip_value(clip, outcome, 0.0)
    # Bottom of the pile by views, top by taste: the answer is neither extreme.
    assert 0.0 < blended < 10.0
    assert blended == pytest.approx(10.0 * (1 - archivist.VIEWS_WEIGHT))


def test_a_song_nobody_published_still_falls_back_to_the_rating(run_id, a_clip):
    with session_scope() as s:
        s.add(Outcome(clip_id=a_clip, rating=8, rated_at=utcnow()))
    with session_scope() as s:
        clip = s.get(Clip, a_clip)
        outcome = s.execute(select(Outcome)
                            .where(Outcome.clip_id == a_clip)).scalar_one()
        assert archivist._clip_value(clip, outcome, None) == 8.0


def test_learning_status_says_when_the_audience_is_leading(run_id):
    assert "producer-only" in archivist.learning_status()["signal"]
    _published_clips([1, 2, 3], run_id)
    status = archivist.learning_status()
    assert "audience-led" in status["signal"]
    assert status["measured"] == 3
