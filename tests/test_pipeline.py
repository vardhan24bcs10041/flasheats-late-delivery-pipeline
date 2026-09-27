"""End-to-end dependability: idempotent reruns, safe failure, replay from preserved raw data.

Runs the real pipeline (real mock API) into a temporary output folder so committed reports are untouched.
"""
import json
from datetime import date

import pytest

RUN_DATE = date(2026, 9, 27)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports" / "latest"))
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    import run_pipeline
    return tmp_path, run_pipeline


def _hashes(tmp):
    return json.loads((tmp / "reports" / "latest" / "run_manifest.json").read_text())["output_hashes"]


def test_rerun_same_date_is_idempotent_and_replay_is_reproducible(isolated):
    tmp, rp = isolated
    assert rp.run(RUN_DATE) == 0
    first = _hashes(tmp)
    journey = tmp / "data" / "gold" / "run_date=2026-09-27" / "order_journey.csv"
    rows_first = sum(1 for _ in open(journey)) - 1

    assert rp.run(RUN_DATE) == 0                               # same logical run again
    assert _hashes(tmp) == first                               # identical output, not doubled
    assert sum(1 for _ in open(journey)) - 1 == rows_first == 1600
    assert len(list((tmp / "data" / "gold").glob("run_date=*"))) == 1   # replaced, not appended

    assert rp.run(RUN_DATE, replay="2026-09-27") == 0          # rebuild without calling the API
    assert _hashes(tmp) == first


def test_schema_break_stops_at_gate_and_publishes_nothing(isolated):
    tmp, rp = isolated
    assert rp.run(RUN_DATE, chaos="missing_column") == 2
    assert not (tmp / "data" / "gold").exists()
    assert not (tmp / "reports" / "latest").exists()
    failed = list((tmp / "data" / "failed_runs").glob("run_date=2026-09-27_*"))
    assert failed and (failed[0] / "validation_report.md").exists()   # the WHY is persisted


def test_failed_run_does_not_overwrite_last_good_output(isolated):
    tmp, rp = isolated
    assert rp.run(RUN_DATE) == 0
    good = _hashes(tmp)
    assert rp.run(RUN_DATE, chaos="stale_data") == 2
    assert _hashes(tmp) == good                                # last good publication untouched


def test_weekly_window_run_scopes_every_source(isolated):
    tmp, rp = isolated
    window = {"from": "2026-08-17", "to": "2026-08-24"}
    assert rp.run(date(2026, 8, 24), window=window) == 0          # same code, different logical week
    journey = tmp / "data" / "gold" / "run_date=2026-08-24" / "order_journey.csv"
    assert sum(1 for _ in open(journey)) - 1 == 403
