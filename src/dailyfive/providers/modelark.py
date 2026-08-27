"""BytePlus ModelArk client — cover art via Seedream.

Image only. ModelArk exposes no text models, which is why the Lyricist runs on
MiniMax instead. Art is generated at 3000x3000: the size every DSP accepts, and
the one that makes the reserved distribution schema meaningful.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..errors import ProviderError
from ..http import request

log = logging.getLogger(__name__)

PROVIDER = "modelark"

# Hard ceiling is 16,777,216 pixels; 3000x3000 is 9,000,000.
COVER_SIZE = "3000x3000"

# 9:16 for the short's reference frame, at the resolution the video model
# generates into. Larger buys nothing: the still is only ever the first frame.
STILL_SIZE = "1080x1920"


class ModelArkClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        cfg = settings()
        self.api_key = api_key or cfg.ark_api_key
        self.base_url = (base_url or cfg.ark_base_url).rstrip("/")
        self.model = model or cfg.ark_image_model
        if not self.api_key:
            raise ProviderError(PROVIDER, "ARK_API_KEY is not set", retryable=False)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def cover(self, prompt: str, *, size: str = COVER_SIZE) -> str:
        """Generate one cover image. Returns a URL valid for ~7 days.

        The caller downloads it immediately — like every other URL in this
        pipeline, it is treated as disposable the moment the bytes are local.
        """
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "watermark": False,
            "n": 1,
        }
        resp = request("POST", f"{self.base_url}/images/generations", provider=PROVIDER,
                       headers=self._headers, json=body, timeout=180.0)
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise ProviderError(PROVIDER, f"images/generations HTTP {resp.status_code}: {detail}",
                                retryable=resp.status_code >= 500, status=resp.status_code)
        data = resp.json()
        items = data.get("data") or []
        if not items or not items[0].get("url"):
            raise ProviderError(PROVIDER, f"no image URL in response: {str(data)[:300]}",
                                retryable=True)
        return items[0]["url"]

    def still(self, prompt: str, *, size: str = STILL_SIZE) -> str:
        """The short's reference frame — the same call, in portrait.

        Separate from :meth:`cover` only in shape, but the shape is the point.
        Every clip in a short animates from this one image, so it has to arrive
        already 9:16: a square still cropped to vertical afterwards loses the
        head or the feet, and the generator would then be animating a frame that
        was never composed for the format.
        """
        return self.cover(prompt, size=size)
