import json
from datetime import date
import pytest
from dailyfive import pipeline as pl
from dailyfive.db import session_scope
from dailyfive.models import Clip, Run
from dailyfive.qc import QCMetrics
from tests.test_end_to_end import wired  # noqa


def test_shortfall_message(wired, monkeypatch):
    calls = {"n": 0}
    def fake_measure(path):
        calls["n"] += 1
        # first 11 clips are dead air -> QC fail; leaves 3 survivors
        bad = calls["n"] <= 11
        return QCMetrics(duration_s=192.0, lufs_i=-11.4, true_peak_db=-0.9,
                         clip_samples=0, silence_ratio=0.9 if bad else 0.02,
                         file_bytes=200_000, measured=True)
    monkeypatch.setattr(pl, "measure", fake_measure)
    pl.run_daily(date(2026, 8, 27))
    with session_scope() as s:
        run = s.query(Run).one()
        print("PHASE", run.phase)
        print("SHORTFALL", json.dumps((run.notes or {}).get("shortfall"), indent=1))
        print("SHIPPED", s.query(Clip).filter(Clip.shipped.is_(True)).count())
