"""Brain routing: any role on any provider, resolved from config alone."""

import pytest

from dailyfive import llm
from dailyfive.config import reload_settings
from dailyfive.errors import ConfigError


@pytest.fixture
def env(monkeypatch):
    def setter(**kw):
        for k in list(kw) + [f"LLM_{r.upper()}" for r in llm.ROLES] + ["LLM_DEFAULT"]:
            # Not delenv: python-dotenv only skips a key already
            # present in os.environ, so a DELETED key is put straight back from
            # the operator's real .env by the reload below — which is how a test
            # asserting the default brain silently asserted whatever the
            # developer happened to be running.
            monkeypatch.setenv(k, "")
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        return reload_settings()
    return setter


def test_default_backs_every_role(env):
    env(LLM_DEFAULT="minimax")
    assert all(b.provider == "minimax" for b in llm.roster().values())


def test_a_single_role_can_be_moved_without_touching_the_others(env):
    env(LLM_DEFAULT="minimax", LLM_PRODUCER="anthropic")
    r = llm.roster()
    assert r["producer"].provider == "anthropic"
    assert r["scout"].provider == "minimax"
    assert r["lyricist"].provider == "minimax"


def test_spec_may_name_a_model(env):
    env(LLM_DEFAULT="minimax:MiniMax-M2.7")
    assert llm.resolve("scout").model == "MiniMax-M2.7"


def test_spec_without_a_model_falls_back_to_the_provider_default(env):
    env(LLM_DEFAULT="minimax", MINIMAX_TEXT_MODEL="MiniMax-M3")
    assert llm.resolve("scout").model == "MiniMax-M3"


def test_openai_dialect_aliases_resolve(env):
    env(LLM_DEFAULT="openai:llama-3.3-70b", LLM_BASE_URL="https://x/api",
        LLM_API_KEY="k")
    b = llm.resolve("scout")
    assert b.provider == "openai-compatible"
    assert b.model == "llama-3.3-70b"
    assert b.base_url == "https://x/api"


def test_unknown_provider_is_rejected_by_name(env):
    env(LLM_DEFAULT="bogus")
    with pytest.raises(ConfigError, match="unknown LLM provider"):
        llm.resolve("scout")


def test_unknown_role_is_rejected(env):
    env(LLM_DEFAULT="minimax")
    with pytest.raises(ConfigError, match="unknown role"):
        llm.resolve("mixing-engineer")


def test_roster_reports_a_bad_config_instead_of_raising(env):
    """The console must render even when the config is wrong."""
    env(LLM_DEFAULT="bogus")
    assert llm.roster()["scout"].provider == "unconfigured"


def test_brains_in_use_lists_only_what_is_needed(env):
    cfg = env(LLM_DEFAULT="minimax", LLM_PRODUCER="anthropic")
    assert cfg.brains_in_use() == {"minimax", "anthropic"}
    cfg = env(LLM_DEFAULT="minimax")
    assert cfg.brains_in_use() == {"minimax"}


def test_think_blocks_are_stripped():
    assert llm._strip_think("<think>reasoning</think>[Verse]\nreal").strip() \
        == "[Verse]\nreal"


def test_json_mode_is_only_offered_where_the_provider_has_one(env, monkeypatch):
    """Anthropic has no JSON mode; asking for one must not send an unknown arg."""
    env(LLM_DEFAULT="anthropic", ANTHROPIC_API_KEY="k")
    seen = {}

    class Fake:
        def complete(self, brain, system, user, *, max_tokens, temperature, json_mode):
            seen["json_mode"] = json_mode
            return "ok"

    monkeypatch.setattr(llm, "_backends", lambda: {"anthropic": Fake()})
    llm.complete("scout", "s", "u", json_mode=True)
    assert seen["json_mode"] is False

    env(LLM_DEFAULT="minimax", MINIMAX_API_KEY="k")
    monkeypatch.setattr(llm, "_backends", lambda: {"minimax": Fake()})
    llm.complete("scout", "s", "u", json_mode=True)
    assert seen["json_mode"] is True


def test_complete_reports_which_brain_answered(env, monkeypatch):
    env(LLM_DEFAULT="minimax", MINIMAX_API_KEY="k", MINIMAX_TEXT_MODEL="MiniMax-M3")

    class Fake:
        def complete(self, brain, system, user, **kw):
            return "hello"

    monkeypatch.setattr(llm, "_backends", lambda: {"minimax": Fake()})
    text, brain = llm.complete("lyricist", "s", "u")
    assert text == "hello"
    assert str(brain) == "minimax:MiniMax-M3"


def test_managed_database_urls_get_a_driver_and_tls():
    """Providers hand out bare postgres:// URLs; SQLAlchemy reaches for
    psycopg2 without an explicit driver and fails on import, not on connect."""
    from dailyfive.config import _normalise_db_url as n

    assert n("postgres://u:p@host:25060/db").startswith("postgresql+psycopg://")
    assert n("postgresql://u:p@host:25060/db").startswith("postgresql+psycopg://")
    assert "sslmode=require" in n("postgres://u:p@host:25060/db")

    # An explicit choice is never overridden.
    assert n("postgresql://u:p@h/db?sslmode=disable").count("sslmode") == 1
    # Local development needs no TLS and often has none available.
    assert "sslmode" not in n("postgresql://u:p@localhost/db")
    # SQLite passes through untouched.
    assert n("sqlite:///x.db") == "sqlite:///x.db"


# ── the Anthropic backend, and the three ways Claude is not an OpenAI endpoint ──
class _FakeMessages:
    def __init__(self, box):
        self.box = box

    def create(self, **kw):
        self.box["sent"] = kw
        return type("R", (), {
            "content": [type("B", (), {"type": "text", "text": "ok"})()],
            "stop_reason": "end_turn", "stop_details": None,
        })()


class _FakeClient:
    def __init__(self, box, **kw):
        box["client_kwargs"] = kw
        self.messages = _FakeMessages(box)


def _anthropic(monkeypatch, **envs):
    """A live-looking Anthropic backend with the SDK stubbed out."""
    import sys
    import types

    from dailyfive import llm
    from dailyfive.config import reload_settings

    box = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    for k, v in envs.items():
        monkeypatch.setenv(k, v)
    reload_settings()
    llm.AnthropicBackend.reset()

    # The client is stubbed; the exception taxonomy is NOT. complete() catches
    # anthropic.APIStatusError and anthropic.APIConnectionError by type, so a
    # module stub that omits them turns every error path into an AttributeError
    # and the tests below would pass for the wrong reason.
    import anthropic as real

    fake = types.ModuleType("anthropic")
    for name in dir(real):
        if name.endswith("Error"):
            setattr(fake, name, getattr(real, name))
    fake.Anthropic = lambda **kw: _FakeClient(box, **kw)
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return llm.AnthropicBackend(), box


def test_temperature_is_never_sent_to_claude(monkeypatch):
    """Current Claude models REMOVED the sampling parameters — temperature,
    top_p and top_k all return a 400. Every agent in this studio passes one
    (1.05 for the Lyricist's second draft, 0.2 for a JSON repair turn), so
    forwarding it would not degrade the switch, it would break every call."""
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)
    backend.complete(Brain("anthropic", "claude-opus-5"), "sys", "usr",
                     max_tokens=2500, temperature=1.05, json_mode=False)
    assert "temperature" not in box["sent"]
    assert "top_p" not in box["sent"] and "top_k" not in box["sent"]


def test_a_tiny_max_tokens_is_raised_to_leave_room_for_thinking(monkeypatch):
    """Thinking is on by default and comes out of the same budget as the answer.
    The Lyricist's forced choice asks for one character with max_tokens=8; under
    adaptive thinking that returns nothing at all."""
    from dailyfive.llm import AnthropicBackend, Brain

    backend, box = _anthropic(monkeypatch)
    backend.complete(Brain("anthropic", "claude-opus-5"), "sys", "usr",
                     max_tokens=8, temperature=0.0, json_mode=False)
    assert box["sent"]["max_tokens"] == AnthropicBackend.MIN_TOKENS


def test_a_generous_max_tokens_is_left_alone(monkeypatch):
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)
    backend.complete(Brain("anthropic", "claude-opus-5"), "sys", "usr",
                     max_tokens=64000, temperature=0.0, json_mode=False)
    assert box["sent"]["max_tokens"] == 64000


def test_the_workspace_header_is_sent_when_configured(monkeypatch):
    """An identity-linked key rejects every request without it — including
    GET /v1/models, so a key cannot be used to discover its own workspace."""
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch, ANTHROPIC_WORKSPACE_ID="wrkspc_123")
    backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                     max_tokens=100, temperature=0.0, json_mode=False)
    assert box["client_kwargs"]["default_headers"] == {
        "anthropic-workspace-id": "wrkspc_123"}


def test_no_workspace_header_is_sent_for_a_classic_key(monkeypatch):
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)
    backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                     max_tokens=100, temperature=0.0, json_mode=False)
    assert box["client_kwargs"]["default_headers"] is None


def test_effort_is_sent_only_when_configured(monkeypatch):
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)
    backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                     max_tokens=100, temperature=0.0, json_mode=False)
    assert "output_config" not in box["sent"]

    backend, box = _anthropic(monkeypatch, ANTHROPIC_EFFORT="medium")
    backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                     max_tokens=100, temperature=0.0, json_mode=False)
    assert box["sent"]["output_config"] == {"effort": "medium"}


def test_a_refusal_is_an_error_and_not_an_empty_string(monkeypatch):
    """A refusal is a 200 with no text. Returning "" from here surfaces three
    layers up as an unparseable JSON body rather than as what it is."""
    import pytest

    from dailyfive.errors import ProviderError
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)

    def refuse(**kw):
        return type("R", (), {
            "content": [], "stop_reason": "refusal",
            "stop_details": type("D", (), {"category": "cyber"})(),
        })()

    backend._get().messages.create = refuse
    with pytest.raises(ProviderError, match="declined"):
        backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                         max_tokens=100, temperature=0.0, json_mode=False)


def test_a_truncated_answer_is_an_error_not_a_partial(monkeypatch):
    """It reaches ask_json as unparseable JSON and burns the repair turn
    re-truncating."""
    import pytest

    from dailyfive.errors import ProviderError
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)
    backend._get().messages.create = lambda **kw: type("R", (), {
        "content": [type("B", (), {"type": "text", "text": "half an ans"})()],
        "stop_reason": "max_tokens", "stop_details": None})()
    with pytest.raises(ProviderError, match="cut off") as exc:
        backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                         max_tokens=100, temperature=0.0, json_mode=False)
    assert exc.value.retryable


def test_a_dropped_connection_to_anthropic_is_retryable(monkeypatch):
    """anthropic.APIConnectionError stringifies to the bare "Connection error.",
    which matches none of the substrings _looks_transient greps for — so before
    the typed chain, a dropped socket was filed as permanent and killed the
    day's run."""
    import pytest

    from dailyfive.errors import ProviderError
    from dailyfive.llm import Brain

    backend, box = _anthropic(monkeypatch)

    def drop(**kw):
        import httpx2

        import anthropic
        raise anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    backend._get().messages.create = drop
    with pytest.raises(ProviderError) as exc:
        backend.complete(Brain("anthropic", "claude-opus-5"), "s", "u",
                         max_tokens=100, temperature=0.0, json_mode=False)
    assert exc.value.retryable


def test_the_effort_setting_is_validated_before_a_credit_is_spent():
    """A typo is a 400 on every call. preflight runs validate_shape, so it fails
    before the day's Suno credits are committed rather than after the Scout."""
    import pytest

    from dailyfive.config import reload_settings
    from dailyfive.errors import ConfigError

    import os
    os.environ["ANTHROPIC_EFFORT"] = "hi"
    try:
        with pytest.raises(ConfigError, match="ANTHROPIC_EFFORT"):
            reload_settings().validate_shape()
    finally:
        os.environ["ANTHROPIC_EFFORT"] = ""
        reload_settings()
