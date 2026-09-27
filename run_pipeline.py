"""One command: EXTRACT -> VALIDATE -> CLEAN -> TRANSFORM -> SAVE (+ LOG, + REPORT).

    python run_pipeline.py --run-date 2026-09-27
    python run_pipeline.py --run-date 2026-09-27 --chaos missing_column      # fails safely
    python run_pipeline.py --run-date 2026-09-27 --replay-raw                # rebuild from preserved raw pages

Exit codes: 0 published · 2 stopped at validation gate · 3 source unavailable · 1 unexpected error.
In every non-zero case NOTHING new is published (bronze raw is kept for debugging when extraction finished).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from pipeline.clean import clean_all
from pipeline.common import load_category_map
from pipeline.config import PROJECT_ROOT, load_config
from pipeline.extract import (SourceUnavailableError, extract_csv, extract_dispatch_api, extract_driver_events,
                              extract_sql, replay_dispatch_from_raw)
from pipeline.logging_utils import build_logger
from pipeline.metrics import compute_metrics
from pipeline.report import validation_markdown, write_reports
from pipeline.save import discard, publish_layer, swap_in, temp_partition, write_json
from pipeline.transform import build_order_journey, build_order_timeline
from pipeline.validate import ValidationError, gate_decision, run_validations

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CHAOS = ["none", "missing_column", "duplicate_order", "stale_data", "api_outage", "schema_drift"]


# ------------------------------------------------------------------------------------------ helpers
def wait_for_health(url: str, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/health", timeout=1).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return False


def maybe_start_mock_api(cfg, logger):
    if not cfg.api["start_mock_api"]:
        logger.info("Mock API autostart disabled")
        return None
    if wait_for_health(cfg.dispatch_api_url, 1):
        logger.info("Dispatch API already running | url=%s", cfg.dispatch_api_url)
        return None
    script = cfg.source_path("dispatch_api_script")
    logger.info("Starting local mock Dispatch API | script=%s", rel(script))
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_for_health(cfg.dispatch_api_url, 12):
        proc.terminate()
        raise SourceUnavailableError("Mock Dispatch API did not become healthy (is port 8000 busy?)")
    logger.info("Mock Dispatch API healthy")
    return proc


def rel(path: Path) -> str:
    """Project-relative path for logs/manifests (absolute if the output dir lives elsewhere)."""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "uncommitted"
    except Exception:
        return "unknown"


def apply_chaos(extracted: dict, scenario: str, logger) -> None:
    """Controlled failures for the classroom/demo — mutate the in-memory extract only."""
    o = extracted["orders"]
    if scenario == "missing_column":
        logger.warning("CHAOS: dropping orders.promised_eta to simulate an upstream schema break")
        o.df = o.df.drop(columns=["promised_eta"])
    elif scenario == "duplicate_order":
        logger.warning("CHAOS: injecting one extra duplicate order row")
        o.df = pd.concat([o.df, o.df.iloc[[0]]], ignore_index=True)
    elif scenario == "stale_data":
        logger.warning("CHAOS: shifting orders.created_at back 365 days to simulate a stale extract")
        ts = pd.to_datetime(o.df["created_at"], format="mixed", errors="coerce") - pd.Timedelta(days=365)
        o.df["created_at"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S")


def frame_hash(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------ run
def run(run_date: date, chaos: str = "none", replay: str | None = None, window: dict | None = None) -> int:
    cfg = load_config()
    rd = run_date.isoformat()
    run_id = uuid.uuid4().hex[:8]
    window = window or {}
    if chaos == "api_outage":
        cfg.raw["sources"]["dispatch_api_url"] = "http://127.0.0.1:8999"   # nothing listens here
        cfg.raw["api"]["start_mock_api"] = False
        cfg.raw["api"]["retry_base_seconds"] = 0.5

    logger = build_logger(cfg.logs_dir / f"pipeline_{rd}.log", cfg.log_level)
    manifest = {"run_id": run_id, "run_date": rd, "started_at": datetime.now().isoformat(timespec="seconds"),
                "chaos": chaos, "replay_raw": replay, "window": window, "git_commit": git_commit(),
                "config_fingerprint": cfg.fingerprint(), "config_overrides": cfg.overrides,
                "config": {"api": cfg.api, "kpi": cfg.kpi, "validation": cfg.validation}}
    logger.info("Pipeline started | run_id=%s run_date=%s chaos=%s replay=%s", run_id, rd, chaos, replay)

    api_proc, bronze_tmp = None, None
    try:
        # ------------------------------------------------------------------ 1. EXTRACT -> bronze
        if not replay:
            api_proc = maybe_start_mock_api(cfg, logger)
        bronze_tmp = temp_partition(cfg.data_dir, "bronze", rd)
        extracted = {e.name: e for e in extract_sql(cfg.source_path("order_db"), bronze_tmp, window, logger)}
        if replay:
            replay_dir = cfg.data_dir / "bronze" / f"run_date={replay}" / "dispatch_api"
            extracted["dispatch"] = replay_dispatch_from_raw(replay_dir, bronze_tmp, logger)
        else:
            extracted["dispatch"] = extract_dispatch_api(cfg, bronze_tmp, logger)
        extracted["driver_events"] = extract_driver_events(cfg.source_path("driver_events"), bronze_tmp, logger)
        iv_path = cfg.source_path("order_interventions")
        if chaos == "schema_drift":
            iv_path = PROJECT_ROOT / "source_systems/_not_ingested/order_interventions_v1_superseded.csv"
            logger.warning("CHAOS: reading the superseded v1 intervention export (older schema)")
        for name, key, system, path in [
            ("support_tickets", "support_tickets", "Support Platform", None),
            ("customer_app_actions", "customer_app_actions", "Customer App analytics", None),
            ("order_interventions", "order_interventions", "Ops intervention log", iv_path),
            ("restaurant_status", "restaurant_status", "Restaurant partner feed", None),
        ]:
            extracted[name] = extract_csv(name, path or cfg.source_path(key), system, bronze_tmp, logger)
        write_json({"run_id": run_id, "sources": [e.manifest_entry() for e in extracted.values()]},
                   bronze_tmp / "_ingest_manifest.json")
        bronze = swap_in(bronze_tmp, cfg.data_dir / "bronze" / f"run_date={rd}")
        bronze_tmp = None
        manifest["sources"] = [e.manifest_entry() for e in extracted.values()]
        logger.info("Bronze preserved | partition=%s", rel(bronze))

        apply_chaos(extracted, chaos, logger)

        # ------------------------------------------------------------------ 2. VALIDATE (gate)
        cmap = load_category_map(PROJECT_ROOT / "config" / "category_map.csv")
        metric_defs = json.loads(cfg.source_path("metric_definitions").read_text(encoding="utf-8"))
        results = run_validations(extracted, cfg, cmap, metric_defs, run_date)
        gate = gate_decision(results)
        for r in results:
            log = logger.error if r.status == "FAIL" else (logger.warning if r.status in ("WARN", "UNKNOWN") else logger.info)
            log("Validation | id=%s status=%s check=%s | %s", r.check_id, r.status, r.check, r.evidence[:180])
        manifest["validation"] = {"counts": gate["counts"], "results": [r.to_dict() for r in results]}
        manifest["gate"] = gate

        if not gate["publish_outputs"]:
            failed = [f"{r.check_id} {r.check}: {r.evidence}" for r in results if r.status == "FAIL"]
            raise ValidationError("; ".join(failed))

        # ------------------------------------------------------------------ 3. CLEAN -> silver
        s = clean_all(extracted, cfg, cmap, results, logger)
        silver = publish_layer(cfg.data_dir, "silver", rd, {k: v for k, v in s.items()})

        # ------------------------------------------------------------------ 4. TRANSFORM -> gold
        journey = build_order_journey(s, cfg, logger)
        timeline = build_order_timeline(s, logger)
        metrics, tables = compute_metrics(journey, s, cfg)

        # ------------------------------------------------------------------ 5. SAVE + REPORT
        gold = publish_layer(cfg.data_dir, "gold", rd, {"order_journey": journey, "order_timeline": timeline, **tables},
                             {"metrics": metrics})
        manifest.update({
            "status": "PUBLISHED", "finished_at": datetime.now().isoformat(timespec="seconds"),
            "outputs": {"bronze": rel(bronze),
                        "silver": rel(silver),
                        "gold": rel(gold), "reports": "reports/latest"},
            "output_hashes": {"order_journey": frame_hash(journey), "order_timeline": frame_hash(timeline),
                              "metrics": hashlib.sha256(json.dumps(metrics, sort_keys=True, default=str).encode()).hexdigest()[:16]},
            "headline": {k: metrics[k].get("value_pct", metrics[k].get("value_min")) for k in ["M1", "M2", "M3", "M4", "M5"]},
        })
        rep_tmp = temp_partition(cfg.reports_dir.parent, "", f"latest_{rd}")
        try:
            write_reports(rep_tmp, journey, metrics, tables, results, gate, manifest, s["issues_log"])
            write_json(metrics, rep_tmp / "metrics.json")
            write_json(manifest, rep_tmp / "run_manifest.json")
            swap_in(rep_tmp, cfg.reports_dir)
        except Exception:
            discard(rep_tmp)
            raise
        write_json(manifest, gold / "run_manifest.json")

        logger.info("Pipeline PUBLISHED | run_id=%s M1_late_rate=%s%% M2_pickup_miss=%s%% gold=%s", run_id,
                    metrics["M1"]["value_pct"], metrics["M2"]["value_pct"], rel(gold))
        print("\nPIPELINE SUCCESS — outputs published")
        print(json.dumps(manifest["headline"], indent=2))
        print("Evidence table: reports/latest/evidence_table.md")
        return 0

    except ValidationError as exc:
        _record_failure(cfg, manifest, rd, run_id, "STOPPED_AT_VALIDATION_GATE", str(exc), logger)
        logger.error("Pipeline stopped at validation gate | %s | no processed output written", exc)
        print(f"\nPIPELINE FAILED at validation gate: {exc}\nNo processed output was published.")
        return 2
    except SourceUnavailableError as exc:
        _record_failure(cfg, manifest, rd, run_id, "SOURCE_UNAVAILABLE", str(exc), logger)
        logger.error("Pipeline stopped: source unavailable | %s | no processed output written", exc)
        print(f"\nPIPELINE FAILED — source unavailable: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        _record_failure(cfg, manifest, rd, run_id, "UNEXPECTED_ERROR", repr(exc), logger)
        logger.exception("Pipeline failed unexpectedly | %s", exc)
        print(f"\nPIPELINE FAILED unexpectedly: {exc}")
        return 1
    finally:
        discard(bronze_tmp)
        if api_proc is not None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                api_proc.kill()


def _record_failure(cfg, manifest, rd, run_id, status, reason, logger):
    """Persist WHY a run failed (the on-call engineer's first question) without touching published data."""
    manifest.update({"status": status, "failure_reason": reason, "finished_at": datetime.now().isoformat(timespec="seconds")})
    out = cfg.data_dir / "failed_runs" / f"run_date={rd}_{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    write_json(manifest, out / "run_manifest.json")
    if "validation" in manifest:
        from pipeline.validate import CheckResult
        results = [CheckResult(**r) for r in manifest["validation"]["results"]]
        (out / "validation_report.md").write_text(validation_markdown(results, manifest["gate"], rd), encoding="utf-8")
    logger.info("Failure evidence written | path=%s", rel(out))


def parse_args():
    p = argparse.ArgumentParser(description="FlashEats late-delivery KPI pipeline")
    p.add_argument("--run-date", default=date.today().isoformat(), help="logical run date YYYY-MM-DD (partition key)")
    p.add_argument("--chaos", default="none", choices=CHAOS, help="controlled failure scenario")
    p.add_argument("--replay-raw", nargs="?", const="__same__", default=None, metavar="RUN_DATE",
                   help="rebuild Dispatch data from preserved raw pages instead of calling the API")
    p.add_argument("--from-date", default=None, help="only orders created on/after this date")
    p.add_argument("--to-date", default=None, help="only orders created before this date")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        rd = date.fromisoformat(args.run_date)
    except ValueError:
        raise SystemExit("--run-date must be YYYY-MM-DD")
    replay = rd.isoformat() if args.replay_raw == "__same__" else args.replay_raw
    raise SystemExit(run(rd, args.chaos, replay, {"from": args.from_date, "to": args.to_date}))
