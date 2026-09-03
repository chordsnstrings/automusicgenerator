"""Phase ordering and resume: a crashed run must not re-spend."""

from datetime import date

from dailyfive import pipeline as pl
from dailyfive.db import session_scope
from dailyfive.models import (Brief, Clip, Job, JobState, Run, RunPhase, Signal,
                              SlotType)


def test_phase_ordering():
    assert pl._before(RunPhase.CREATED, RunPhase.SENSED)
    assert not pl._before(RunPhase.SHIPPED, RunPhase.SENSED)
    assert pl._before(RunPhase.FAILED, RunPhase.SENSED), \
        "a failed run must re-enter the phase chain"


def test_preflight_reports_every_problem_at_once():
    """One error listing four problems beats four runs each dying on the next."""
    problems = pl.preflight(require_ffmpeg=True)
    assert len(problems) >= 2
    joined = " ".join(problems)
    assert "SUNO_API_KEY" in joined, "a missing Suno key must be named"
    assert "personas" in joined, "an unregistered cast must be named"
    assert "SPACES_" in joined, "delivering to the filesystem must be flagged"


def test_opening_the_same_date_twice_resumes_rather_than_duplicating():
    a, _ = pl._open_run(date(2026, 8, 27), resume=True)
    b, _ = pl._open_run(date(2026, 8, 27), resume=True)
    assert a == b
    with session_scope() as s:
        assert s.query(Run).count() == 1


def test_no_resume_refuses_an_existing_run():
    pl._open_run(date(2026, 8, 27), resume=True)
    try:
        pl._open_run(date(2026, 8, 27), resume=False)
    except RuntimeError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_failed_run_resumes_from_what_the_database_actually_shows(run_id,
                                                                 brief_factory):
    """Phase is inferred from rows, not trusted from the column."""
    bid = brief_factory()
    with session_scope() as s:
        s.add(Job(run_id=run_id, brief_id=bid, idempotency_key="k",
                  task_id="task-1", state=JobState.SUCCESS, payload={}))
        s.get(Run, run_id).phase = RunPhase.FAILED
    _, phase = pl._open_run(date(2026, 8, 27), resume=True)
    assert phase == RunPhase.SUBMITTED


def test_inferred_phase_walks_back_to_created_on_an_empty_run(run_id):
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.CREATED


def test_inferred_phase_recognises_each_stage(run_id, brief_factory):
    with session_scope() as s:
        s.add(Signal(run_id=run_id, rank=1, theme="t", sentiment="s"))
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.SENSED

    bid = brief_factory()
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.WRITTEN  # fixture sets lyrics

    with session_scope() as s:
        j = Job(run_id=run_id, brief_id=bid, idempotency_key="k2",
                task_id="t1", state=JobState.SUCCESS, payload={})
        s.add(j)
        s.flush()
        job_id = j.id
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.SUBMITTED

    with session_scope() as s:
        s.add(Clip(run_id=run_id, job_id=job_id, brief_id=bid, audio_id="a1",
                   slot_type=SlotType.FULL, local_path="/tmp/x.mp3"))
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.RENDERED


def test_brief_dict_round_trips_the_fields_agents_need(run_id, brief_factory):
    bid = brief_factory(title="Slow Burn", bpm=84)
    with session_scope() as s:
        d = pl._brief_dict(s.get(Brief, bid))
    for key in ("title", "theme", "slot_type", "bpm", "style_string", "lyrics"):
        assert key in d
    assert d["slot_type"] == "full" and d["bpm"] == 84


def test_a_lane_that_cannot_be_filled_is_recorded_not_silent(run_id, brief_factory):
    """Shipping one against a contract of five must not read as a clean day."""
    from dailyfive.config import settings
    from dailyfive.models import Clip, Job, JobState, SlotType

    bid = brief_factory(slot="full")
    with session_scope() as s:
        s.get(Brief, bid).dropped_reason = "clearance: drug trafficking narrative"
        j = Job(run_id=run_id, brief_id=bid, idempotency_key="k9",
                state=JobState.MIRRORED, payload={})
        s.add(j); s.flush()
        s.add(Clip(run_id=run_id, job_id=j.id, brief_id=bid, audio_id="x",
                   slot_type=SlotType.FULL, qc_verdict="fail",
                   qc_reason="38% of the track is silence"))

    pl._record_shortfall(run_id, [{"clip_id": 1, "slot_type": "full"}], settings())

    with session_scope() as s:
        sf = (s.get(Run, run_id).notes or {}).get("shortfall")
    assert sf, "an unfilled lane must be recorded on the run"
    assert sf["gaps"]["full"] == settings().full_slots - 1
    joined = " ".join(sf["causes"])
    assert "drug trafficking" in joined, "the cause must name the dropped brief"
    assert "cut by QC" in joined


def test_no_shortfall_note_when_every_lane_fills(run_id):
    from dailyfive.config import settings
    cfg = settings()
    picks = ([{"clip_id": i, "slot_type": "full"} for i in range(cfg.full_slots)]
             + [{"clip_id": 100 + i, "slot_type": "short"} for i in range(cfg.short_slots)])
    pl._record_shortfall(run_id, picks, cfg)
    with session_scope() as s:
        assert not (s.get(Run, run_id).notes or {}).get("shortfall")


def test_delivery_falls_back_to_the_filesystem_without_a_bucket(monkeypatch, tmp_path):
    """A first run must be possible before DigitalOcean is set up."""
    from dailyfive.config import reload_settings
    from dailyfive.storage import LocalStore, open_store
    for var in ("SPACES_KEY", "SPACES_SECRET", "SPACES_BUCKET"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("AUDIO_STORE", "")
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    reload_settings()

    store = open_store()
    assert isinstance(store, LocalStore)
    store.check_access()

    key = store.key_for("2026-08-27", "01_slug", "meta.json")
    assert key == "songs/2026-08-27/01_slug/meta.json"
    store.put_text('{"ok":true}', key)
    assert store.exists(key)
    assert store.signed_url(key).startswith("file://")


def test_a_configured_bucket_still_wins(monkeypatch):
    from dailyfive.config import reload_settings
    from dailyfive.storage import open_store
    monkeypatch.setenv("SPACES_KEY", "k")
    monkeypatch.setenv("SPACES_SECRET", "s")
    monkeypatch.setenv("SPACES_BUCKET", "b")
    monkeypatch.setenv("SPACES_ENDPOINT", "https://nyc3.digitaloceanspaces.com")
    monkeypatch.setenv("AUDIO_STORE", "")
    reload_settings()
    assert type(open_store()).__name__ == "Spaces"


def test_local_and_spaces_agree_on_key_layout(monkeypatch, tmp_path):
    """Same keys either way, so a local run can be copied up untouched."""
    from dailyfive.config import reload_settings
    from dailyfive.storage import LocalStore
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    reload_settings()
    assert LocalStore().key_for("2026-08-27", "manifest.json") == \
        "songs/2026-08-27/manifest.json"


def test_every_third_party_import_is_a_declared_dependency():
    """A module that works on a dev machine because it was pip-installed by
    hand is a crash on the first clean build. alembic was exactly that."""
    import ast
    import pathlib
    import sys
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    meta = tomllib.load(open(root / "pyproject.toml", "rb"))["project"]
    declared = set()
    for spec in meta["dependencies"] + sum(meta.get("optional-dependencies", {}).values(), []):
        declared.add(spec.split(">=")[0].split("[")[0].split("==")[0].strip().lower())

    alias = {"dotenv": "python-dotenv", "botocore": "boto3"}
    stdlib = set(sys.stdlib_module_names)
    missing = {}
    for f in (root / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m and m not in stdlib and m != "dailyfive":
                    if alias.get(m, m).lower() not in declared:
                        missing.setdefault(m, str(f.relative_to(root)))
    assert not missing, f"undeclared dependencies: {missing}"


def test_one_unstorable_artifact_does_not_fail_a_run_that_shipped(run_id):
    """On 2026-08-27 all five songs were delivered and the run was then marked
    FAILED because a single stored_files INSERT lost its connection — leaving a
    red run, a red health check, and five finished songs that were fine.

    The failure is recorded rather than swallowed: it lands in the run's notes,
    and `keys` simply lacks the entry, which is already how the manifest and the
    day page express "this file is not there".
    """
    from dailyfive.db import session_scope
    from dailyfive.models import Run
    from dailyfive.pipeline import _note_unstored

    _note_unstored(run_id, ["01_song/master.wav: connection lost"])
    _note_unstored(run_id, ["02_song/cover.jpg: connection lost"])

    with session_scope() as s:
        run = s.get(Run, run_id)
        files = run.notes["unstored"]["files"]
    # Appended, not replaced: two clips each losing a file in the same run is
    # the difference between "one file went missing" and "something is wrong
    # with the database tonight", and the second note must not erase the first.
    assert len(files) == 2
    assert any("master.wav" in f for f in files)
    assert any("cover.jpg" in f for f in files)


def test_the_ship_loop_keeps_going_after_a_store_failure():
    """The try/except is the point — without it the first failed upload aborts
    delivery for every clip after it, too."""
    import inspect

    from dailyfive import pipeline
    src = inspect.getsource(pipeline._phase_ship)
    upload_block = src[src.index("keys: dict[str, str] = {}"):]
    assert "except Exception" in upload_block
    assert "_note_unstored" in upload_block


def test_preflight_refuses_an_anthropic_key_with_no_workspace(monkeypatch):
    """A key alone is not enough. Without the workspace header every request is
    refused — including the one that would tell you the workspace — so a
    preflight that passes without it green-lights a day that 400s on the
    Scout's first call, after the Suno credits are committed."""
    from dailyfive.config import reload_settings
    from dailyfive.pipeline import preflight

    monkeypatch.setenv("LLM_DEFAULT", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "")
    reload_settings()
    assert any("ANTHROPIC_WORKSPACE_ID" in p for p in preflight(require_ffmpeg=False))

    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_1")
    reload_settings()
    assert not any("ANTHROPIC_WORKSPACE_ID" in p for p in preflight(require_ffmpeg=False))
