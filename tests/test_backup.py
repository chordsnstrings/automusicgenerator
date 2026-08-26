"""The backup, and the one property that makes it a backup: it is elsewhere.

The failure this pins is not a crash. `dailyfive backup` printed a key and
exited zero the whole time it was writing the dump into a row of the cluster it
was a dump of — so the log said the backup worked, every night, and there was
no off-site copy of anything.
"""

from __future__ import annotations

import gzip

from dailyfive import backup
from dailyfive.db import session_scope
from dailyfive.models import StoredFile


def test_the_backup_is_never_written_into_the_database_it_dumps(monkeypatch):
    """AUDIO_STORE defaults to `database`, which is the value in .env.example
    and in both components of deploy/app-platform.json. Reusing the pipeline's
    own open_store() here therefore returned a DatabaseStore and put the dump
    in stored_files. Two failures at once: no disaster recovery, because losing
    the cluster loses every backup with it; and compounding growth, because the
    dump contains stored_files, so each night's dump contains every retained
    night before it.
    """
    monkeypatch.setenv("AUDIO_STORE", "database")
    from dailyfive.config import reload_settings
    reload_settings()

    with session_scope() as s:
        before = s.query(StoredFile).count()

    key = backup.to_storage()

    assert key is None, "a key implies an off-site copy that does not exist"
    with session_scope() as s:
        assert s.query(StoredFile).count() == before, \
            "the dump was stored inside the database it is a dump of"


def test_a_local_only_backup_still_exists_on_disk(monkeypatch):
    """Refusing the wrong destination must not mean refusing to dump. The local
    copy survives a bad migration, which is the failure it is actually for."""
    monkeypatch.setenv("AUDIO_STORE", "database")
    from dailyfive.config import reload_settings
    cfg = reload_settings()

    assert backup.to_storage() is None
    dumps = sorted((cfg.work_dir / "backups").glob("dailyfive-*"))
    assert len(dumps) == 1
    with gzip.open(dumps[0], "rb") as fh:
        assert fh.read(16), "the dump is empty"


def test_the_backup_goes_to_spaces_when_spaces_is_configured(monkeypatch):
    """The destination is chosen on one question — is it somewhere else — and
    not on which store the pipeline happens to be using for audio."""
    sent = {}

    class FakeSpaces:
        def upload(self, path, key, **kw):
            sent["path"], sent["key"] = path, key
            return key

    monkeypatch.setenv("AUDIO_STORE", "database")
    monkeypatch.setenv("SPACES_BUCKET", "songs")
    monkeypatch.setenv("SPACES_KEY", "k")
    monkeypatch.setenv("SPACES_SECRET", "s")
    monkeypatch.setenv("SPACES_ENDPOINT", "https://blr1.digitaloceanspaces.com")
    from dailyfive.config import reload_settings
    cfg = reload_settings()
    monkeypatch.setattr("dailyfive.storage.Spaces", lambda: FakeSpaces())

    key = backup.to_storage()
    assert key == f"{cfg.spaces_prefix}/_backups/{sent['path'].name}"
    assert "_backups/" in sent["key"]
    with session_scope() as s:
        assert s.query(StoredFile).count() == 0


def test_local_copies_are_still_pruned_when_there_is_nowhere_to_upload(monkeypatch):
    """Refusing the destination must not skip the pruning. Returning early on a
    missing destination would leave every dump ever taken on the container's
    disk, which is the failure this call is nominally preventing."""
    monkeypatch.setenv("AUDIO_STORE", "database")
    from dailyfive.config import reload_settings
    cfg = reload_settings()

    stale = cfg.work_dir / "backups"
    stale.mkdir(parents=True, exist_ok=True)
    for day in range(4):
        (stale / f"dailyfive-2026080{day}T000000Z.sqlite.gz").write_bytes(b"old")

    assert backup.to_storage(keep_local=2) is None
    assert len(list(stale.glob("dailyfive-*"))) == 2
