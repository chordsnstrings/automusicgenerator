"""Getting the finished files in front of people, and reading back what happened.

This is the half of the studio that was missing. Everything upstream optimises
against a number somebody typed in — the Producer's score, then your 1-to-10 —
and the whole premise of the thing is that music does not sell, it gets views.
A rating is a guess about what an audience will do. A view count is what they
did.

Two platforms, two very different APIs, one shape:

    publish(clip, platform)   upload the file, record a Publication row
    refresh(days)             ask each platform what its videos have done

The shape matters more than either implementation. Anything that can upload a
file and report a number can be added beside these two without the Archivist
learning about it, because the Archivist reads ``Publication`` and never a
provider.

Nothing here runs without credentials the operator supplies. There is no
fallback, no stub that pretends to publish, and no code path that reports
success without an id from the platform — a publishing system that quietly
does nothing is worse than one that is switched off, because the metrics
that never arrive look like songs nobody watched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from ..db import session_scope
from ..errors import ProviderError
from ..models import Publication, utcnow

log = logging.getLogger(__name__)

PLATFORMS = ("youtube", "tiktok")


@dataclass
class Uploaded:
    """What a platform gives back when it has taken the file."""
    external_id: str
    url: str
    extra: dict


def _backend(platform: str):
    if platform == "youtube":
        from . import youtube
        return youtube
    if platform == "tiktok":
        from . import tiktok
        return tiktok
    raise ProviderError("publish", f"unknown platform {platform!r} — "
                                   f"known: {', '.join(PLATFORMS)}", retryable=False)


def publish(*, clip_id: int, platform: str, file: Path, title: str,
            description: str = "", tags: list[str] | None = None,
            privacy: str = "public") -> Publication:
    """Upload one file to one platform and record what came back.

    The row is written BEFORE the upload starts and updated after, which is the
    same discipline the Conductor uses for Suno jobs: a process that dies
    mid-upload leaves a `pending` row naming what it was doing, rather than a
    gap that the next run reads as "never tried".

    A second call for the same clip and platform is refused rather than retried
    when the first one succeeded. An upload has no idempotency key — the
    platform will happily take the same file twice and give it two ids — so the
    only thing standing between a timeout and a duplicate posting is this
    check.
    """
    backend = _backend(platform)
    file = Path(file)
    if not file.is_file():
        raise ProviderError("publish", f"nothing to upload at {file}", retryable=False)

    with session_scope() as s:
        row = s.execute(select(Publication).where(
            Publication.clip_id == clip_id,
            Publication.platform == platform)).scalar_one_or_none()
        if row is not None and row.status == "live":
            raise ProviderError("publish",
                                f"clip {clip_id} is already on {platform} as "
                                f"{row.external_id}", retryable=False)
        if row is None:
            row = Publication(clip_id=clip_id, platform=platform)
            s.add(row)
        row.status = "pending"
        row.error = None
        s.flush()
        pub_id = row.id

    try:
        got = backend.upload(file, title=title, description=description,
                             tags=tags or [], privacy=privacy)
    except Exception as exc:
        with session_scope() as s:
            row = s.get(Publication, pub_id)
            row.status = "failed"
            row.error = str(exc)[:2000]
        log.error("publishing clip %d to %s failed: %s", clip_id, platform, exc)
        raise

    with session_scope() as s:
        row = s.get(Publication, pub_id)
        row.status = "live"
        row.external_id = got.external_id
        row.url = got.url
        row.metrics = {"upload": got.extra}
        row.published_at = utcnow()
        s.flush()
        s.expunge(row)
    log.info("clip %d is live on %s: %s", clip_id, platform, got.url)
    return row


def refresh(*, days: int = 90, platform: str | None = None) -> dict:
    """Ask each platform what its videos have done, and store the answer.

    Bounded by age rather than run unbounded, because the interesting window is
    short: a short either travels in its first fortnight or it does not, and
    re-querying a two-year-old video every night spends quota to watch a number
    that stopped moving. Ninety days is generous about where that line is.
    """
    cutoff = utcnow() - timedelta(days=days)
    out: dict = {"checked": 0, "updated": 0, "failed": [], "by_platform": {}}

    with session_scope() as s:
        rows = s.execute(select(Publication.id, Publication.platform,
                                Publication.external_id)
                         .where(Publication.status == "live",
                                Publication.external_id.isnot(None),
                                Publication.published_at >= cutoff)).all()

    wanted: dict[str, list[tuple[int, str]]] = {}
    for pub_id, plat, ext in rows:
        if platform and plat != platform:
            continue
        wanted.setdefault(plat, []).append((pub_id, ext))

    for plat, items in wanted.items():
        out["checked"] += len(items)
        try:
            stats = _backend(plat).statistics([ext for _id, ext in items])
        except Exception as exc:
            # One platform being down or out of quota must not cost the other
            # its readings — they are independent numbers about different
            # audiences and there is no reason to lose both.
            log.error("could not read %s metrics: %s", plat, exc)
            out["failed"].append({"platform": plat, "error": str(exc)[:300]})
            continue

        got = 0
        with session_scope() as s:
            for pub_id, ext in items:
                m = stats.get(ext)
                if not m:
                    continue
                row = s.get(Publication, pub_id)
                row.views = m.get("views")
                row.likes = m.get("likes")
                row.comments = m.get("comments")
                row.shares = m.get("shares")
                row.metrics = {**(row.metrics or {}), "latest": m}
                row.metrics_at = utcnow()
                got += 1
        out["updated"] += got
        out["by_platform"][plat] = got

    return out


def for_clip(clip_id: int) -> list[dict]:
    """Every publication of one song, for the day page and the CLI."""
    with session_scope() as s:
        rows = s.execute(select(Publication)
                         .where(Publication.clip_id == clip_id)
                         .order_by(Publication.platform)).scalars().all()
        return [{
            "platform": r.platform, "status": r.status, "url": r.url,
            "external_id": r.external_id, "views": r.views, "likes": r.likes,
            "comments": r.comments, "shares": r.shares,
            "published_at": r.published_at, "metrics_at": r.metrics_at,
            "error": r.error,
        } for r in rows]
