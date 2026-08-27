"""The short's assembly line, and the allowance it is not allowed to waste.

Three generations a day is the whole budget, and a short spends two of them. So
the properties worth a test here are mostly about *not* spending: an impossible
duration is refused before it is submitted, a cached still or clip is reused, and
one failed take does not throw away the one that worked.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dailyfive import shorts
from dailyfive.errors import ProviderError
from dailyfive.providers import minimax

BRIEF = {"title": "Two Days", "theme": "waiting on a reply", "bpm": 112,
         "style_string": "uptempo alt-pop, 808s", "hook_note": "hook at 0:07"}


# ── the model matrix ─────────────────────────────────────────────────────────
def test_an_impossible_duration_is_refused_before_it_is_submitted():
    """A rejected job does not give the allowance back, so this is checked here
    rather than discovered at the provider."""
    with pytest.raises(ProviderError) as exc:
        minimax.check_duration("MiniMax-Hailuo-2.3", "1080P", 10)
    assert "1080P" in str(exc.value)


def test_the_documented_combinations_pass():
    minimax.check_duration("MiniMax-Hailuo-2.3", "768P", 10)
    minimax.check_duration("MiniMax-Hailuo-2.3", "768P", 6)
    minimax.check_duration("MiniMax-Hailuo-2.3", "1080P", 6)


def test_an_unknown_model_is_warned_about_rather_than_blocked(caplog):
    """This table goes stale. Refusing a combination the vendor has since added
    would be worse than sending one it rejects."""
    import logging
    with caplog.at_level(logging.WARNING):
        minimax.check_duration("MiniMax-Hailuo-9", "768P", 15)
    assert any("matrix" in r.getMessage() for r in caplog.records)


def test_the_prompt_optimiser_is_off(monkeypatch):
    """It is on by default and rewrites the prompt before generating — which
    would put the cast's fixed terms through a rewriter nobody reads."""
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"task_id": "t1", "base_resp": {"status_code": 0}}

    def fake(method, url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    monkeypatch.setattr(minimax, "request", fake)
    client = minimax.MiniMaxVideoClient(api_key="k")
    client.submit("a prompt", first_frame="data:image/jpeg;base64,AA", duration=10)
    assert sent["prompt_optimizer"] is False
    assert sent["duration"] == 10


def test_wait_polls_until_the_file_exists(monkeypatch):
    steps = iter([("Queueing", ""), ("Processing", ""), ("Success", "f9")])
    client = minimax.MiniMaxVideoClient(api_key="k")
    monkeypatch.setattr(client, "poll", lambda t: next(steps))
    monkeypatch.setattr(client, "download_url", lambda f: f"https://x/{f}")
    assert client.wait("t1", sleep=lambda _s: None) == "https://x/f9"


def test_wait_gives_up_on_a_terminal_failure(monkeypatch):
    client = minimax.MiniMaxVideoClient(api_key="k")
    monkeypatch.setattr(client, "poll", lambda t: ("Fail", ""))
    with pytest.raises(ProviderError) as exc:
        client.wait("t1", sleep=lambda _s: None)
    assert not exc.value.retryable


# ── assembly ─────────────────────────────────────────────────────────────────
@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Everything that costs money, replaced by something that writes a file."""
    calls = {"stills": 0, "submits": [], "cut": {}}

    class FakeArk:
        def still(self, prompt, **kw):
            calls["stills"] += 1
            calls["still_prompt"] = prompt
            return "https://ark/still.jpg"

    class FakeVideo:
        def submit(self, prompt, *, first_frame, duration, resolution="768P"):
            calls["submits"].append(prompt)
            calls["first_frame"] = first_frame[:32]
            return f"task{len(calls['submits'])}"

        def wait(self, task_id, **kw):
            return f"https://mm/{task_id}.mp4"

    def fake_download(url, dest, **kw):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 20_000)
        return 20_000

    def fake_cut(clips, audio, dest, **kw):
        calls["cut"] = {"clips": list(clips), **kw}
        Path(dest).write_bytes(b"mp4")
        return dest

    monkeypatch.setattr(shorts, "ModelArkClient", lambda *a, **k: FakeArk())
    monkeypatch.setattr(shorts, "MiniMaxVideoClient", lambda *a, **k: FakeVideo())
    monkeypatch.setattr(shorts, "download", fake_download)
    monkeypatch.setattr(shorts.video, "hook_short", fake_cut)
    monkeypatch.setattr(shorts.videodirector, "plan",
                        lambda *a, **k: [
                            {"framing": "wide", "move": "locked",
                             "action": "she counts in", "why_it_follows": ""},
                            {"framing": "close", "move": "push",
                             "action": "she turns", "why_it_follows": ""}])
    return calls


def test_a_short_is_built_from_two_takes(wired, tmp_path):
    out = shorts.make(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                      run_date=date(2026, 8, 27), dest=tmp_path / "short.mp4",
                      work=tmp_path / "w")
    assert out.path.is_file()
    assert len(out.clips) == 2
    assert len(wired["submits"]) == 2
    assert out.performer


def test_the_still_is_generated_once_and_reused(wired, tmp_path):
    kw = dict(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
              run_date=date(2026, 8, 27), work=tmp_path / "w")
    shorts.make(dest=tmp_path / "a.mp4", **kw)
    second = shorts.make(dest=tmp_path / "b.mp4", **kw)
    assert wired["stills"] == 1
    assert "still" in second.reused
    # And the clips too — this is the property that stops a restarted process
    # spending a whole day's allowance a second time.
    assert len(wired["submits"]) == 2
    assert "clip0" in second.reused and "clip1" in second.reused


def test_force_spends_the_allowance_again(wired, tmp_path):
    kw = dict(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
              run_date=date(2026, 8, 27), work=tmp_path / "w")
    shorts.make(dest=tmp_path / "a.mp4", **kw)
    shorts.make(dest=tmp_path / "b.mp4", force=True, **kw)
    assert wired["stills"] == 2
    assert len(wired["submits"]) == 4


def test_one_failed_take_still_produces_a_short(wired, tmp_path, monkeypatch):
    """The allowance is spent either way, so failing the build over one take
    would throw away a generation that worked."""
    class HalfBroken:
        n = 0

        def submit(self, prompt, **kw):
            HalfBroken.n += 1
            return f"task{HalfBroken.n}"

        def wait(self, task_id, **kw):
            if task_id == "task1":
                raise ProviderError("minimax-video", "moderation", retryable=False)
            return "https://mm/ok.mp4"

    monkeypatch.setattr(shorts, "MiniMaxVideoClient", lambda *a, **k: HalfBroken())
    out = shorts.make(clip_id=8, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                      run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                      work=tmp_path / "w")
    assert len(out.clips) == 1
    assert out.path.is_file()


def test_no_takes_at_all_is_an_error_not_an_empty_file(wired, tmp_path, monkeypatch):
    class Broken:
        def submit(self, prompt, **kw):
            return "t"

        def wait(self, task_id, **kw):
            raise ProviderError("minimax-video", "no allowance left", retryable=False)

    monkeypatch.setattr(shorts, "MiniMaxVideoClient", lambda *a, **k: Broken())
    with pytest.raises(ProviderError):
        shorts.make(clip_id=9, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                    run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                    work=tmp_path / "w")


def test_the_cut_covers_exactly_the_footage_that_exists(wired, tmp_path):
    """Twenty seconds of audio over twenty seconds of clips: the cut never has
    to hold a frame or drop one."""
    shorts.make(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                work=tmp_path / "w", shots=2, seconds=10)
    assert wired["cut"]["duration_s"] == 20.0
    assert wired["cut"]["bpm"] == 112


def test_the_still_prompt_is_the_cast_s_and_carries_the_fixed_terms(wired, tmp_path):
    from dailyfive.cast import FIXED_TERMS
    shorts.make(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                work=tmp_path / "w")
    assert wired["still_prompt"].endswith(FIXED_TERMS)
    for prompt in wired["submits"]:
        assert prompt.endswith(FIXED_TERMS)


def test_the_first_frame_is_sent_as_bytes_not_as_the_providers_url(wired, tmp_path):
    """ModelArk's link lives about a week and would make the generation depend
    on a third party fetching a fourth at an unpredictable moment."""
    shorts.make(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                work=tmp_path / "w")
    assert wired["first_frame"].startswith("data:image/jpeg;base64,")


def test_a_cached_shot_list_of_the_wrong_length_is_replanned(wired, tmp_path):
    work = tmp_path / "w"
    work.mkdir(parents=True)
    (work / "shots.json").write_text(json.dumps([{"framing": "wide", "move": "locked",
                                                  "action": "one only"}]))
    out = shorts.make(clip_id=7, brief=BRIEF, audio=tmp_path / "m.wav", lrc=None,
                      run_date=date(2026, 8, 27), dest=tmp_path / "s.mp4",
                      work=work, shots=2)
    assert len(out.shots) == 2
    assert "shots" not in out.reused
