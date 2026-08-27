"""MiniMax clients — the Lyricist's model, and the short's footage.

Uses the OpenAI-compatible surface for full structural control over the lyric,
with ``/v1/lyrics_generation`` as a fallback when the chat surface is
unavailable. Like Suno, v1 endpoints return HTTP 200 on failure, so the
``base_resp`` envelope is checked rather than the status code.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import settings
from ..errors import ProviderError
from ..http import request

log = logging.getLogger(__name__)

PROVIDER = "minimax"

BASE_RESP_CODES = {
    1000: ("unknown error", True),
    1001: ("timeout", True),
    1002: ("rate limited", True),
    1004: ("auth failed — key lacks scope for this surface", False),
    1008: ("insufficient balance", False),
    1026: ("content flagged by moderation", False),
    2013: ("invalid parameters", False),
    2049: ("invalid api key", False),
    # Verified live: the video surface answers 2056 once the day's generations
    # are gone, and it answers it AFTER parameter validation — a bad
    # duration/resolution pair comes back as 2013 and costs nothing. Not
    # retryable, because the thing to wait for is tomorrow, not a backoff.
    2056: ("plan usage limit reached — the day's video allowance is spent", False),
}


class MiniMaxClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        cfg = settings()
        self.api_key = api_key or cfg.minimax_api_key
        self.base_url = (base_url or cfg.minimax_base_url).rstrip("/")
        self.model = model or cfg.minimax_text_model
        if not self.api_key:
            raise ProviderError(PROVIDER, "MINIMAX_API_KEY is not set", retryable=False)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    @staticmethod
    def _check_base_resp(body: dict[str, Any], what: str) -> None:
        br = body.get("base_resp") or {}
        code = br.get("status_code", 0)
        if code in (0, None):
            return
        msg, retryable = BASE_RESP_CODES.get(code, (br.get("status_msg", "unknown"), False))
        raise ProviderError(PROVIDER, f"{what}: {msg} (status_code {code})", retryable=retryable)

    def chat(self, system: str, user: str, *, max_tokens: int = 4000,
             temperature: float = 0.9) -> str:
        """One completion on the OpenAI-compatible surface.

        Thinking is disabled explicitly: reasoning tokens on a lyric write are
        pure cost, and the ``<think>`` wrapper would have to be stripped anyway.
        """
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "thinking": {"type": "disabled"},
        }
        resp = request("POST", f"{self.base_url}/v1/chat/completions", provider=PROVIDER,
                       headers=self._headers, json=body, timeout=180.0)
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"chat HTTP {resp.status_code}: {resp.text[:300]}",
                                retryable=resp.status_code >= 500, status=resp.status_code)
        data = resp.json()
        self._check_base_resp(data, "chat")
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(PROVIDER, f"chat returned no choices: {str(data)[:300]}",
                                retryable=True)
        content = (choices[0].get("message") or {}).get("content") or ""
        return _strip_think(content).strip()

    def lyrics(self, prompt: str) -> str:
        """Fallback path: MiniMax writes a whole song from a theme description."""
        body = {"mode": "write_full_song", "prompt": prompt}
        resp = request("POST", f"{self.base_url}/v1/lyrics_generation", provider=PROVIDER,
                       headers=self._headers, json=body, timeout=180.0)
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"lyrics HTTP {resp.status_code}: {resp.text[:300]}",
                                retryable=resp.status_code >= 500, status=resp.status_code)
        data = resp.json()
        self._check_base_resp(data, "lyrics_generation")
        # The field has moved between versions; be liberal about where it lands.
        for path in (("lyrics",), ("data", "lyrics"), ("data", "text")):
            node: Any = data
            for key in path:
                node = (node or {}).get(key) if isinstance(node, dict) else None
            if isinstance(node, str) and node.strip():
                return node.strip()
        raise ProviderError(PROVIDER, f"no lyrics in response: {str(data)[:300]}", retryable=True)


def _strip_think(text: str) -> str:
    """Remove ``<think>…</think>`` blocks that some surfaces emit regardless."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


# ── video ────────────────────────────────────────────────────────────────────
# A separate client rather than more methods on the one above, because the two
# surfaces share only a vendor and a key. Everything that matters — the model
# names, the failure modes, the fact that one is a poll and the other is not —
# is different, and folding them together would put a text model's name in a
# video request the first time someone reused `self.model`.

VIDEO_PROVIDER = "minimax-video"

# What each model will actually accept, verified against the live surface: a
# 10-second job at 1080P comes back "model MiniMax-Hailuo-2.3 does not support
# the combination of duration 10s and resolution 1080P", and the same job at
# 768P passes validation and fails only on the allowance.
#
# Encoded rather than remembered, because the assembler was once written for
# three-second clips on an assumption that was never checked against a matrix.
# An unlisted model is passed through with a warning: this table can go stale,
# and refusing a combination the vendor has since added would be worse than
# sending one it rejects.
DURATIONS: dict[str, dict[str, tuple[int, ...]]] = {
    "MiniMax-Hailuo-2.3": {"768P": (6, 10), "1080P": (6,)},
    "MiniMax-Hailuo-2.3-Fast": {"768P": (6, 10), "1080P": (6,)},
    "MiniMax-Hailuo-02": {"768P": (6, 10), "1080P": (6,)},
}

# Terminal and non-terminal states. "Fail" carries no reason on this surface,
# which is why the poller logs the whole envelope rather than the status word.
PENDING = ("Preparing", "Queueing", "Processing", "")
DONE = "Success"


def check_duration(model: str, resolution: str, duration: int) -> None:
    """Refuse an impossible combination before it costs a day's allowance."""
    allowed = DURATIONS.get(model, {}).get(resolution)
    if allowed is None:
        log.warning("no duration matrix for %s at %s — sending %ds unchecked",
                    model, resolution, duration)
        return
    if duration not in allowed:
        raise ProviderError(
            VIDEO_PROVIDER,
            f"{model} at {resolution} accepts {allowed} seconds, not {duration}",
            retryable=False)


class MiniMaxVideoClient:
    """Image-to-video. Submit, poll, then resolve a file id to a download URL."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        cfg = settings()
        self.api_key = api_key or cfg.minimax_api_key
        self.base_url = (base_url or cfg.minimax_base_url).rstrip("/")
        self.model = model or cfg.minimax_video_model
        if not self.api_key:
            raise ProviderError(VIDEO_PROVIDER, "MINIMAX_API_KEY is not set",
                                retryable=False)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def submit(self, prompt: str, *, first_frame: str, duration: int = 10,
               resolution: str = "768P") -> str:
        """Start one generation. Returns a task id.

        ``prompt_optimizer`` is off, and that is a safety decision rather than a
        quality one. It is on by default and it rewrites the prompt before
        generating — which would put the cast's fixed terms, the clause that
        keeps the depiction adult and clothed, through a rewriter whose output
        nobody sees. A prompt that cannot be edited after it is written is the
        entire reason those terms are appended last.
        """
        check_duration(self.model, resolution, duration)
        body = {
            "model": self.model,
            "prompt": prompt,
            "first_frame_image": first_frame,
            "duration": duration,
            "resolution": resolution,
            "prompt_optimizer": False,
        }
        resp = request("POST", f"{self.base_url}/v1/video_generation",
                       provider=VIDEO_PROVIDER, headers=self._headers,
                       json=body, timeout=120.0)
        data = _json(resp, "video_generation")
        MiniMaxClient._check_base_resp(data, "video_generation")
        task_id = data.get("task_id")
        if not task_id:
            raise ProviderError(VIDEO_PROVIDER,
                                f"no task_id in response: {str(data)[:300]}",
                                retryable=True)
        log.info("minimax video task %s submitted (%s, %ds, %s)",
                 task_id, self.model, duration, resolution)
        return str(task_id)

    def poll(self, task_id: str) -> tuple[str, str]:
        """One status check. Returns (status, file_id); file_id is empty until done."""
        resp = request("GET", f"{self.base_url}/v1/query/video_generation",
                       provider=VIDEO_PROVIDER, headers=self._headers,
                       params={"task_id": task_id}, timeout=60.0)
        data = _json(resp, "query/video_generation")
        MiniMaxClient._check_base_resp(data, "query/video_generation")
        return str(data.get("status") or ""), str(data.get("file_id") or "")

    def download_url(self, file_id: str) -> str:
        """Resolve a finished file id to a URL. Treated as disposable, like every
        other provider URL here — the caller fetches it immediately."""
        resp = request("GET", f"{self.base_url}/v1/files/retrieve",
                       provider=VIDEO_PROVIDER, headers=self._headers,
                       params={"file_id": file_id}, timeout=60.0)
        data = _json(resp, "files/retrieve")
        MiniMaxClient._check_base_resp(data, "files/retrieve")
        url = (data.get("file") or {}).get("download_url")
        if not url:
            raise ProviderError(VIDEO_PROVIDER,
                                f"no download_url for file {file_id}: {str(data)[:300]}",
                                retryable=True)
        return str(url)

    def wait(self, task_id: str, *, timeout_s: float = 900.0,
             interval_s: float = 10.0,
             sleep=time.sleep) -> str:
        """Poll to completion and return the download URL.

        The timeout is generous because a queued job on the free allowance can
        sit for several minutes before it starts, and giving up on it does not
        give the allowance back.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            status, file_id = self.poll(task_id)
            if status == DONE and file_id:
                return self.download_url(file_id)
            if status not in PENDING:
                raise ProviderError(VIDEO_PROVIDER,
                                    f"task {task_id} ended as {status!r}",
                                    retryable=False)
            if time.monotonic() >= deadline:
                raise ProviderError(VIDEO_PROVIDER,
                                    f"task {task_id} still {status!r} after "
                                    f"{timeout_s:.0f}s", retryable=True)
            sleep(interval_s)


def _json(resp, what: str) -> dict:
    """These endpoints answer 200 on failure, so the status code is only half of it."""
    if resp.status_code >= 400:
        raise ProviderError(VIDEO_PROVIDER, f"{what} HTTP {resp.status_code}: "
                                            f"{resp.text[:300]}",
                            retryable=resp.status_code >= 500,
                            status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(VIDEO_PROVIDER, f"{what}: non-JSON body "
                                            f"{resp.text[:200]!r}",
                            retryable=True) from exc
