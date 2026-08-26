"""MiniMax client — the Lyricist's model.

Uses the OpenAI-compatible surface for full structural control over the lyric,
with ``/v1/lyrics_generation`` as a fallback when the chat surface is
unavailable. Like Suno, v1 endpoints return HTTP 200 on failure, so the
``base_resp`` envelope is checked rather than the status code.
"""

from __future__ import annotations

import logging
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
