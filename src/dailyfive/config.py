"""Settings, read once from the environment.

Deliberately a plain dataclass rather than a settings framework: this runs
unattended on a small droplet, and a missing key should fail loudly at startup
with the name of the variable, not three frames deep inside a generation call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

load_dotenv()


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    raw = _s(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _b(name: str, default: bool = False) -> bool:
    raw = _s(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Suno
    suno_api_key: str = field(default_factory=lambda: _s("SUNO_API_KEY"))
    suno_base_url: str = field(default_factory=lambda: _s("SUNO_BASE_URL", "https://api.sunoapi.org").rstrip("/"))
    suno_model: str = field(default_factory=lambda: _s("SUNO_MODEL", "V5_5"))

    public_base_url: str = field(default_factory=lambda: _s("PUBLIC_BASE_URL").rstrip("/"))
    webhook_secret: str = field(default_factory=lambda: _s("WEBHOOK_SECRET"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _s("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _s("ANTHROPIC_MODEL", "claude-opus-5"))

    # MiniMax
    minimax_api_key: str = field(default_factory=lambda: _s("MINIMAX_API_KEY"))
    minimax_base_url: str = field(default_factory=lambda: _s("MINIMAX_BASE_URL", "https://api.minimax.io").rstrip("/"))
    minimax_text_model: str = field(default_factory=lambda: _s("MINIMAX_TEXT_MODEL", "MiniMax-M3"))

    # ModelArk
    ark_api_key: str = field(default_factory=lambda: _s("ARK_API_KEY"))
    ark_base_url: str = field(default_factory=lambda: _s(
        "ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/"))
    ark_image_model: str = field(default_factory=lambda: _s("ARK_IMAGE_MODEL", "seedream-5-0-260128"))

    # Spaces
    spaces_key: str = field(default_factory=lambda: _s("SPACES_KEY"))
    spaces_secret: str = field(default_factory=lambda: _s("SPACES_SECRET"))
    spaces_region: str = field(default_factory=lambda: _s("SPACES_REGION", "nyc3"))
    spaces_bucket: str = field(default_factory=lambda: _s("SPACES_BUCKET"))
    spaces_endpoint: str = field(default_factory=lambda: _s("SPACES_ENDPOINT").rstrip("/"))
    spaces_prefix: str = field(default_factory=lambda: _s("SPACES_PREFIX", "songs").strip("/"))
    spaces_public_index: bool = field(default_factory=lambda: _b("SPACES_PUBLIC_INDEX"))

    # Signals
    youtube_api_key: str = field(default_factory=lambda: _s("YOUTUBE_API_KEY"))
    lastfm_api_key: str = field(default_factory=lambda: _s("LASTFM_API_KEY"))
    genius_token: str = field(default_factory=lambda: _s("GENIUS_ACCESS_TOKEN"))
    reddit_client_id: str = field(default_factory=lambda: _s("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _s("REDDIT_CLIENT_SECRET"))
    reddit_user_agent: str = field(default_factory=lambda: _s("REDDIT_USER_AGENT", "dailyfive/0.3"))
    signal_region: str = field(default_factory=lambda: _s("SIGNAL_REGION", "US"))

    # Database
    database_url: str = field(default_factory=lambda: _s("DATABASE_URL", "sqlite:///dailyfive.db"))

    # Run shape
    full_briefs: int = field(default_factory=lambda: _i("FULL_BRIEFS", 4))
    full_slots: int = field(default_factory=lambda: _i("FULL_SLOTS", 3))
    short_briefs: int = field(default_factory=lambda: _i("SHORT_BRIEFS", 3))
    short_slots: int = field(default_factory=lambda: _i("SHORT_SLOTS", 2))
    short_duration_s: int = field(default_factory=lambda: _i("SHORT_DURATION_S", 45))
    daily_credit_cap: int = field(default_factory=lambda: _i("DAILY_CREDIT_CAP", 800))
    # Polling cadence. Generation takes 60-90s, so a 20s interval notices
    # promptly without hammering the API. Tests drop these to zero.
    poll_interval_s: int = field(default_factory=lambda: _i("POLL_INTERVAL_S", 20))
    generation_timeout_s: int = field(default_factory=lambda: _i("GENERATION_TIMEOUT_S", 900))
    wav_timeout_s: int = field(default_factory=lambda: _i("WAV_TIMEOUT_S", 600))
    work_dir: Path = field(default_factory=lambda: Path(_s("WORK_DIR", "./work")).expanduser())

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def total_briefs(self) -> int:
        return self.full_briefs + self.short_briefs

    @property
    def total_slots(self) -> int:
        return self.full_slots + self.short_slots

    def callback_url(self, kind: str) -> str:
        """Callback Suno posts to. The secret is a path segment, not a query
        param, so it stays out of access logs that only record the path prefix."""
        if not self.public_base_url:
            raise ConfigError("PUBLIC_BASE_URL is required — Suno must be able to reach you")
        secret = self.webhook_secret or "nosecret"
        return f"{self.public_base_url}/webhooks/{secret}/{kind}"

    def require(self, *names: str) -> None:
        """Fail at the top of a run, naming every missing key at once.

        One error listing four missing variables beats four consecutive runs
        each dying on the next one.
        """
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            env_names = {
                "suno_api_key": "SUNO_API_KEY",
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "minimax_api_key": "MINIMAX_API_KEY",
                "ark_api_key": "ARK_API_KEY",
                "spaces_key": "SPACES_KEY",
                "spaces_secret": "SPACES_SECRET",
                "spaces_bucket": "SPACES_BUCKET",
                "spaces_endpoint": "SPACES_ENDPOINT",
                "public_base_url": "PUBLIC_BASE_URL",
            }
            pretty = ", ".join(env_names.get(m, m.upper()) for m in missing)
            raise ConfigError(f"missing required settings: {pretty}")

    def validate_shape(self) -> None:
        if self.full_slots > self.full_briefs:
            raise ConfigError(
                f"FULL_SLOTS ({self.full_slots}) exceeds FULL_BRIEFS ({self.full_briefs}) — "
                "there would be no surplus for the Producer to choose from")
        if self.short_slots > self.short_briefs:
            raise ConfigError(
                f"SHORT_SLOTS ({self.short_slots}) exceeds SHORT_BRIEFS ({self.short_briefs})")
        if not 10 <= self.short_duration_s <= 360:
            raise ConfigError("SHORT_DURATION_S must be between 10 and 360 (Suno V5_5 limit)")


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Re-read the environment. Used by tests; never called during a run."""
    global _settings
    load_dotenv(override=True)
    _settings = Settings()
    return _settings
