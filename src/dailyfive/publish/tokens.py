"""Access tokens that outlive the process, and refresh tokens that rotate.

Both platforms hand out a short-lived access token and a long-lived refresh
token, and both are seeded once by the operator from an OAuth consent they
complete in a browser. After that this module owns them.

The reason they are in the database rather than the environment is TikTok:
every refresh issues a NEW refresh token and invalidates the one used. A
credential held in an env var therefore works exactly once — the first refresh
succeeds, the rotated token has nowhere to go, and the next one fails with a
token that is no longer valid. Google's refresh token does not rotate, but
storing the two differently would be a difference nobody remembers.

Seeding is one-way on purpose: an env var is read only when the table has
nothing for that platform. Otherwise a stale value left in the environment
would overwrite a rotated token on every restart, which is the same failure
with more steps.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from ..db import session_scope
from ..errors import ProviderError
from ..models import OAuthToken, utcnow

log = logging.getLogger(__name__)

# Refreshed this far before the platform's own expiry, so a long upload started
# with a valid token does not finish with an expired one.
EARLY_S = 300.0


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    account_id: str | None


def _seed_env(platform: str) -> str | None:
    """The refresh token the operator pasted in, if any."""
    return os.environ.get(f"{platform.upper()}_REFRESH_TOKEN", "").strip() or None


def load(platform: str) -> OAuthToken | None:
    with session_scope() as s:
        row = s.execute(select(OAuthToken)
                        .where(OAuthToken.platform == platform)).scalar_one_or_none()
        if row is None:
            return None
        s.expunge(row)
        return row


def save(platform: str, *, access_token: str, refresh_token: str | None,
         expires_in: float | None, account_id: str | None = None,
         scope: str | None = None) -> None:
    """Persist what a refresh returned.

    ``refresh_token`` is only overwritten when the platform actually sent one.
    Google omits it from a refresh response, and treating "absent" as "cleared"
    would log the studio out of YouTube the first time it renewed an access
    token.
    """
    with session_scope() as s:
        row = s.execute(select(OAuthToken)
                        .where(OAuthToken.platform == platform)).scalar_one_or_none()
        if row is None:
            row = OAuthToken(platform=platform)
            s.add(row)
        row.access_token = access_token
        if refresh_token:
            row.refresh_token = refresh_token
        if expires_in:
            row.expires_at = utcnow() + timedelta(seconds=float(expires_in))
        if account_id:
            row.account_id = account_id
        if scope:
            row.scope = scope


def current(platform: str, refresher) -> Token:
    """A usable access token for this platform, refreshing if it has to.

    ``refresher`` is the platform's own exchange, passed in rather than
    imported, so this module knows about token lifecycles and nothing about
    either API.
    """
    row = load(platform)
    seed = _seed_env(platform)

    if row is None and not seed:
        raise ProviderError(platform,
                            f"no credentials for {platform}. Complete the OAuth "
                            f"consent once and set {platform.upper()}_REFRESH_TOKEN, "
                            f"or run `dailyfive publish auth {platform}`.",
                            retryable=False)

    if row is not None and row.access_token and row.expires_at:
        if (row.expires_at - utcnow()).total_seconds() > EARLY_S:
            return Token(row.access_token, row.refresh_token, row.account_id)

    refresh_token = (row.refresh_token if row is not None else None) or seed
    if not refresh_token:
        raise ProviderError(platform, f"{platform} has no refresh token stored "
                                      f"and none in the environment",
                            retryable=False)

    got = refresher(refresh_token)
    save(platform, access_token=got["access_token"],
         refresh_token=got.get("refresh_token"),
         expires_in=got.get("expires_in"),
         account_id=got.get("account_id"), scope=got.get("scope"))
    log.info("%s access token refreshed", platform)
    return Token(got["access_token"],
                 got.get("refresh_token") or refresh_token,
                 got.get("account_id"))
