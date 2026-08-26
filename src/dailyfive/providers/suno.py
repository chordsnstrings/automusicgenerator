"""sunoapi.org client.

Two things about this API drive the design of everything above it.

It returns HTTP 200 with a non-200 ``code`` in the body on several failures, so
``_unwrap`` is the only place a response is trusted. And it is asynchronous with
an at-most-three-retries callback, so every state query has a polling twin —
:meth:`record_info` — that the Conductor falls back to when a webhook never
lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..errors import BudgetExceeded, ProviderError, RejectedContent
from ..http import request

log = logging.getLogger(__name__)

PROVIDER = "suno"

# Per-model limits, enforced by the Compiler before a request leaves the
# building rather than discovered as silent truncation afterwards.
MODEL_LIMITS: dict[str, dict[str, int]] = {
    "V4":       {"prompt": 3000, "style": 200,  "title": 80},
    "V4_5":     {"prompt": 5000, "style": 1000, "title": 100},
    "V4_5PLUS": {"prompt": 5000, "style": 1000, "title": 100},
    "V4_5ALL":  {"prompt": 5000, "style": 1000, "title": 80},
    "V5":       {"prompt": 5000, "style": 1000, "title": 100},
    "V5_5":     {"prompt": 5000, "style": 1000, "title": 100},
}

# Only V5_5 accepts an explicit duration.
DURATION_MODELS = {"V5_5"}

# code -> (message, retryable)
ERROR_CODES = {
    400: ("invalid parameters", False),
    401: ("unauthorized — check SUNO_API_KEY", False),
    404: ("not found", False),
    405: ("rate limit exceeded", True),
    409: ("record already exists", False),
    422: ("validation error", False),
    451: ("access authorization failure", False),
    413: ("prompt or theme too long", False),
    429: ("insufficient credits", False),
    430: ("call frequency too high", True),
    455: ("system maintenance", True),
    500: ("server error", True),
}


@dataclass(slots=True)
class SunoClip:
    """One of the two clips a generation returns."""
    audio_id: str
    audio_url: str | None
    stream_audio_url: str | None
    image_url: str | None
    title: str | None
    tags: str | None
    duration: float | None
    model_name: str | None
    prompt: str | None

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> "SunoClip":
        return cls(
            audio_id=str(d.get("id") or d.get("audioId") or ""),
            audio_url=d.get("audio_url") or d.get("audioUrl") or d.get("source_audio_url"),
            stream_audio_url=d.get("stream_audio_url") or d.get("streamAudioUrl"),
            image_url=d.get("image_url") or d.get("imageUrl"),
            title=d.get("title"),
            tags=d.get("tags"),
            duration=_as_float(d.get("duration")),
            model_name=d.get("model_name") or d.get("modelName"),
            prompt=d.get("prompt"),
        )


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class SunoClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        cfg = settings()
        self.api_key = api_key or cfg.suno_api_key
        self.base_url = (base_url or cfg.suno_base_url).rstrip("/")
        if not self.api_key:
            raise ProviderError(PROVIDER, "SUNO_API_KEY is not set", retryable=False)

    # ── plumbing ─────────────────────────────────────────────────────────────
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _call(self, method: str, path: str, *, json: dict | None = None,
              params: dict | None = None, timeout: float = 60.0) -> Any:
        resp = request(method, f"{self.base_url}{path}", provider=PROVIDER,
                       headers=self._headers, json=json, params=params, timeout=timeout)
        return self._unwrap(resp, path)

    @staticmethod
    def _unwrap(resp, path: str) -> Any:
        """The only place a Suno response is believed.

        HTTP 200 with ``code: 429`` in the body is a real thing this API does,
        so the envelope is checked before the status.
        """
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(PROVIDER, f"{path}: non-JSON response "
                                          f"(HTTP {resp.status_code}): {resp.text[:200]}",
                                retryable=resp.status_code >= 500, status=resp.status_code)

        code = body.get("code", resp.status_code)
        if code == 200:
            return body.get("data")

        msg, retryable = ERROR_CODES.get(code, (body.get("msg") or "unknown error", code >= 500))
        detail = f"{path}: {msg} (code {code})"
        if code == 429:
            raise BudgetExceeded(f"suno: {detail}")
        raise ProviderError(PROVIDER, detail, retryable=retryable,
                            status=resp.status_code, code=code)

    # ── endpoints ────────────────────────────────────────────────────────────
    def credits(self) -> int:
        """Free. Called before and after every run so spend is always known."""
        data = self._call("GET", "/api/v1/generate/credit", timeout=20.0)
        if isinstance(data, dict):
            data = data.get("credits", data.get("data", 0))
        return int(data or 0)

    def generate(self, payload: dict) -> str:
        """Submit a generation. Returns the taskId.

        The payload is built by the Compiler, not here — this client validates
        shape but does not invent parameters.
        """
        data = self._call("POST", "/api/v1/generate", json=payload, timeout=90.0)
        task_id = (data or {}).get("taskId") or (data or {}).get("task_id")
        if not task_id:
            raise ProviderError(PROVIDER, f"generate returned no taskId: {data}", retryable=True)
        return str(task_id)

    def record_info(self, task_id: str) -> dict:
        """The poll half of the callback pair.

        Suno gives up on a webhook after three failed deliveries. Without this,
        that run is simply lost, with no error raised anywhere.
        """
        data = self._call("GET", "/api/v1/generate/record-info",
                          params={"taskId": task_id}, timeout=30.0)
        return data or {}

    def wav_generate(self, task_id: str, audio_id: str) -> str:
        """Request true WAV conversion for one clip. Returns a new taskId."""
        payload = {"taskId": task_id, "audioId": audio_id,
                   "callBackUrl": settings().callback_url("wav")}
        data = self._call("POST", "/api/v1/wav/generate", json=payload, timeout=60.0)
        wav_task = (data or {}).get("taskId") or (data or {}).get("task_id")
        if not wav_task:
            raise ProviderError(PROVIDER, f"wav/generate returned no taskId: {data}", retryable=True)
        return str(wav_task)

    def wav_record_info(self, wav_task_id: str) -> dict:
        data = self._call("GET", "/api/v1/wav/record-info",
                          params={"taskId": wav_task_id}, timeout=30.0)
        return data or {}

    def timestamped_lyrics(self, task_id: str, audio_id: str) -> list[dict]:
        """Word-level alignment, used to write the .lrc.

        Instrumental tracks return nothing, which is not an error.
        """
        try:
            data = self._call("POST", "/api/v1/generate/get-timestamped-lyrics",
                              json={"taskId": task_id, "audioId": audio_id}, timeout=60.0)
        except ProviderError as exc:
            log.warning("timestamped lyrics unavailable for %s: %s", audio_id, exc)
            return []
        return (data or {}).get("alignedWords") or []

    def create_persona(self, task_id: str, audio_id: str, *, name: str,
                       description: str, style: str | None = None,
                       vocal_start: float = 0.0, vocal_end: float = 30.0) -> str:
        """Build a reusable persona from a finished generation. Synchronous.

        The vocal segment must be 10-30 seconds and the source generation must
        have completed. Each audio_id can back exactly one persona, so a 409
        here means the persona already exists rather than that anything failed.
        """
        span = vocal_end - vocal_start
        if not 10.0 <= span <= 30.0:
            raise ProviderError(PROVIDER,
                                f"vocal segment is {span:.1f}s; must be 10-30s",
                                retryable=False)
        payload = {
            "taskId": task_id, "audioId": audio_id,
            "name": name[:80], "description": description[:900],
            "vocalStart": round(vocal_start, 2), "vocalEnd": round(vocal_end, 2),
        }
        if style:
            payload["style"] = style[:200]
        data = self._call("POST", "/api/v1/generate/generate-persona",
                          json=payload, timeout=120.0)
        persona_id = (data or {}).get("personaId")
        if not persona_id:
            raise ProviderError(PROVIDER, f"generate-persona returned no personaId: {data}",
                                retryable=True)
        return str(persona_id)

    def generate_lyrics(self, prompt: str) -> str:
        """Fallback lyricist. Prompt caps at 200 characters here."""
        payload = {"prompt": prompt[:200], "callBackUrl": settings().callback_url("lyrics")}
        data = self._call("POST", "/api/v1/lyrics", json=payload, timeout=60.0)
        return str((data or {}).get("taskId") or "")


def parse_status(status: str | None) -> tuple[str, bool]:
    """Map a Suno status string to (kind, retryable).

    Unknown values are treated as retryable failures rather than being silently
    ignored, so a new status appears in the job log instead of stalling a run.
    """
    from ..models import SUNO_FAILURE_STATUSES
    if not status:
        return ("unknown", True)
    if status in SUNO_FAILURE_STATUSES:
        return SUNO_FAILURE_STATUSES[status]
    return ("unknown", True)


def check_moderation(status: str | None) -> None:
    if status == "SENSITIVE_WORD_ERROR":
        raise RejectedContent("Suno moderation refused the lyrics or style string")
