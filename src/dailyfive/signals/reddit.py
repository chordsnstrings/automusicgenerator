"""Reddit — discourse and sentiment, which is where this stack is strongest.

Leading, because people argue about a sound well before it charts. Weak on
what the sound actually is, which is the Music Director's problem to solve.

Uses the application-only OAuth flow: a client credentials grant, no user
account involved, 100 requests per minute.
"""

from __future__ import annotations

import time

from ..config import settings
from ..http import request
from . import FeedResult

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"

SUBS = ["popheads", "hiphopheads", "listentothis", "electronicmusic",
        "indieheads", "WeAreTheMusicMakers"]

_token: tuple[str, float] | None = None


def _access_token() -> str | None:
    """Cached for its lifetime minus a minute. Returns None if unconfigured."""
    global _token
    cfg = settings()
    if not (cfg.reddit_client_id and cfg.reddit_client_secret):
        return None
    if _token and _token[1] > time.time():
        return _token[0]

    import base64
    basic = base64.b64encode(
        f"{cfg.reddit_client_id}:{cfg.reddit_client_secret}".encode()).decode()
    import httpx
    resp = httpx.post(TOKEN_URL,
                      headers={"Authorization": f"Basic {basic}",
                               "User-Agent": cfg.reddit_user_agent},
                      data={"grant_type": "client_credentials"}, timeout=30.0)
    if resp.status_code != 200:
        return None
    body = resp.json()
    tok = body.get("access_token")
    if not tok:
        return None
    _token = (tok, time.time() + int(body.get("expires_in", 3600)) - 60)
    return tok


def fetch(per_sub: int = 8) -> FeedResult:
    cfg = settings()
    try:
        token = _access_token()
    except Exception as exc:
        return FeedResult("reddit", "leading", error=f"auth failed: {str(exc)[:160]}")
    if not token:
        return FeedResult("reddit", "leading",
                          error="REDDIT_CLIENT_ID/SECRET not set or auth rejected")

    headers = {"Authorization": f"Bearer {token}", "User-Agent": cfg.reddit_user_agent}
    items: list[dict] = []
    failed: list[str] = []

    for sub in SUBS:
        try:
            resp = request("GET", f"{API}/r/{sub}/hot", provider="reddit", headers=headers,
                           params={"limit": per_sub, "raw_json": 1}, timeout=25.0, attempts=1)
            if resp.status_code >= 400:
                failed.append(sub)
                continue
            for child in (resp.json().get("data") or {}).get("children") or []:
                d = child.get("data") or {}
                if d.get("stickied"):
                    continue
                items.append({
                    "sub": sub,
                    "title": d.get("title"),
                    "score": d.get("score", 0),
                    "comments": d.get("num_comments", 0),
                    "flair": d.get("link_flair_text"),
                })
        except Exception:
            failed.append(sub)

    if not items:
        return FeedResult("reddit", "leading", error=f"no posts (failed subs: {failed})")
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return FeedResult("reddit", "leading", items=items[:40])
