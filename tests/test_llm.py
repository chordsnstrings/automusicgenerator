"""Brain routing: any role on any provider, resolved from config alone."""

import pytest

from dailyfive import llm
from dailyfive.config import reload_settings
from dailyfive.errors import ConfigError


@pytest.fixture
def env(monkeypatch):
    def setter(**kw):
        for k in list(kw) + [f"LLM_{r.upper()}" for r in llm.ROLES] + ["LLM_DEFAULT"]:
            monkeypatch.delenv(k, raising=False)
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
