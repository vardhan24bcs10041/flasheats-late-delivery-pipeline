"""EXTRACT — four retrieval modes, each proving it retrieved everything.

  SQL   Order Platform DB      orders, restaurants, drivers (customers skipped: PII, not needed)
  API   Dispatch Service       paginated REST, bounded retries on transient errors only
  JSON  Driver App export      nested driver -> events, flattened
  CSV   Support / Customer app / Ops interventions / Restaurant status exports

Every source is landed untouched in the bronze partition before anything else happens,
so a later bug can always be fixed and re-run from the raw copy.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests


class SourceUnavailableError(RuntimeError):
    """A source could not be read at all (file missing, API down after retries)."""


class RetryableAPIError(SourceUnavailableError):
    pass


class NonRetryableAPIError(SourceUnavailableError):
    pass


@dataclass
class Extracted:
    name: str
    mode: str                     # SQL | API | JSON | CSV
    system: str                   # owning client system
    df: pd.DataFrame
    completeness: dict = field(default_factory=dict)
    raw_files: list = field(default_factory=list)

    def manifest_entry(self) -> dict:
        return {
            "source": self.name,
            "mode": self.mode,
            "system": self.system,
            "rows": int(len(self.df)),
            "completeness": self.completeness,
            "raw_files": self.raw_files,
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _raw_ref(path: Path, root: Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)[:16]}


# ---------------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------------
def build_orders_sql(from_date: str | None, to_date: str | None):
    """Date-window query so the same code can run for any logical day/week."""
    where, params = [], []
    if from_date:
        where.append("created_at >= ?")
        params.append(from_date)
    if to_date:
        where.append("created_at < ?")
        params.append(to_date)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return f"SELECT * FROM orders{clause}", f"SELECT COUNT(*) FROM orders{clause}", params


def extract_sql(db_path: Path, bronze: Path, window: dict, logger) -> list[Extracted]:
    if not db_path.exists():
        raise SourceUnavailableError(f"Order DB not found: {db_path}")

    out_dir = bronze / "order_db"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    orders_sql, count_sql, params = build_orders_sql(window.get("from"), window.get("to"))
    queries = {
        "orders": (orders_sql, count_sql, params),
        "restaurants": ("SELECT * FROM restaurants", "SELECT COUNT(*) FROM restaurants", []),
        "drivers": ("SELECT * FROM drivers", "SELECT COUNT(*) FROM drivers", []),
    }

    with sqlite3.connect(db_path) as con:
        for name, (q, cq, p) in queries.items():
            df = pd.read_sql(q, con, params=p, dtype=str if name == "orders" else None)
            expected = con.execute(cq, p).fetchone()[0]
            raw_path = out_dir / f"{name}.csv"
            df.to_csv(raw_path, index=False)
            (out_dir / f"{name}.sql").write_text(q + f"\n-- params={p}\n", encoding="utf-8")
            results.append(Extracted(
                name=name, mode="SQL", system="Order Platform DB",
                df=df,
                completeness={"source_count": int(expected), "retrieved": int(len(df)),
                              "complete": int(expected) == len(df)},
                raw_files=[_raw_ref(raw_path, bronze)],
            ))
            logger.info("Extracted SQL | table=%s rows=%s source_count=%s", name, len(df), expected)
    return results


# ---------------------------------------------------------------------------------
# API (paginated, bounded retries, raw page preservation)
# ---------------------------------------------------------------------------------
def request_page(session, url, page, page_size, *, max_retries, base_seconds, timeout,
                 retryable_status, logger, sleep=time.sleep, stats=None):
    """Fetch one page. Retry only transient failures; fail fast on everything else."""
    stats = stats if stats is not None else {}
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(f"{url}/dispatch/orders",
                                   params={"page": page, "page_size": page_size},
                                   timeout=timeout)
        except requests.RequestException as exc:
            wait = base_seconds * (2 ** (attempt - 1))
            logger.warning("Dispatch API unreachable | page=%s attempt=%s/%s wait=%.1fs error=%s",
                           page, attempt, max_retries, wait, type(exc).__name__)
            stats["retries"] = stats.get("retries", 0) + 1
            if attempt < max_retries:
                sleep(wait)
                continue
            raise RetryableAPIError(
                f"Dispatch API unreachable on page {page} after {max_retries} attempts "
                f"({type(exc).__name__}). No output published. Re-run later, or re-run with "
                f"--replay-raw to rebuild from the last preserved raw pages.") from exc

        if response.status_code == 200:
            return response.json()

        if response.status_code in retryable_status:
            retry_after = response.headers.get("Retry-After")
            if response.status_code == 429:
                try:
                    retry_after = response.json().get("retry_after_seconds", retry_after)
                except ValueError:
                    pass
            wait = float(retry_after) if retry_after is not None else base_seconds * (2 ** (attempt - 1))
            logger.warning("Retryable dispatch API failure | page=%s status=%s attempt=%s/%s wait=%.1fs",
                           page, response.status_code, attempt, max_retries, wait)
            stats["retries"] = stats.get("retries", 0) + 1
            stats.setdefault("transient_errors", []).append({"page": page, "status": response.status_code,
                                                             "attempt": attempt})
            if attempt < max_retries:
                sleep(wait)
                continue
            raise RetryableAPIError(f"Dispatch API page {page} still failing after {max_retries} attempts "
                                    f"(last status {response.status_code}). No output published.")

        raise NonRetryableAPIError(f"Dispatch API returned non-retryable status {response.status_code} "
                                   f"on page {page} — needs a code/credential fix, not a retry.")
    raise RetryableAPIError(f"Dispatch API page {page}: retries exhausted")  # defensive


def extract_dispatch_api(cfg, bronze: Path, logger, session=None, sleep=time.sleep) -> Extracted:
    api = cfg.api
    out_dir = bronze / "dispatch_api"
    out_dir.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()

    rows, raw_files, expected_total, page, stats = [], [], None, 1, {"retries": 0}
    while True:
        payload = request_page(session, cfg.dispatch_api_url, page, api["page_size"],
                               max_retries=api["max_retries"], base_seconds=api["retry_base_seconds"],
                               timeout=api["timeout_seconds"], retryable_status=set(api["retryable_status"]),
                               logger=logger, sleep=sleep, stats=stats)
        raw_path = out_dir / f"dispatch_page_{page:03d}.json"
        raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")   # preserve BEFORE use
        raw_files.append(_raw_ref(raw_path, bronze))

        if expected_total is None:
            expected_total = int(payload.get("total_records", 0))
        rows.extend(payload.get("data", []))
        logger.info("Fetched dispatch page | page=%s rows=%s cumulative=%s has_more=%s",
                    page, len(payload.get("data", [])), len(rows), payload.get("has_more"))
        if not payload.get("has_more", False):
            break
        page += 1

    return _dispatch_result(rows, expected_total, page, raw_files, stats, "live API")


def replay_dispatch_from_raw(replay_dir: Path, bronze: Path, logger) -> Extracted:
    """Rebuild dispatch data from preserved raw pages (API outage / reproducibility)."""
    pages = sorted(replay_dir.glob("dispatch_page_*.json"))
    if not pages:
        raise SourceUnavailableError(f"No preserved dispatch pages found in {replay_dir}")
    out_dir = bronze / "dispatch_api"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, raw_files, expected_total = [], [], None
    for p in pages:
        payload = json.loads(p.read_text(encoding="utf-8"))
        target = out_dir / p.name
        if p.resolve() != target.resolve():
            shutil.copy2(p, target)
        raw_files.append(_raw_ref(target, bronze))
        expected_total = expected_total or int(payload.get("total_records", 0))
        rows.extend(payload.get("data", []))
    logger.warning("Dispatch data REPLAYED from preserved raw pages | source=%s pages=%s rows=%s",
                   replay_dir, len(pages), len(rows))
    return _dispatch_result(rows, expected_total, len(pages), raw_files, {"retries": 0}, f"replay:{replay_dir.name}")


def _dispatch_result(rows, expected_total, pages, raw_files, stats, via) -> Extracted:
    df = pd.DataFrame(rows)
    unique_ids = int(df["order_id"].nunique()) if "order_id" in df else 0
    return Extracted(
        name="dispatch", mode="API", system="Dispatch Service", df=df,
        completeness={"expected_total": expected_total, "retrieved": len(df), "unique_order_ids": unique_ids,
                      "pages": pages, "retries": stats.get("retries", 0),
                      "transient_errors": stats.get("transient_errors", []),
                      "retrieved_via": via,
                      "complete": expected_total == len(df) == unique_ids},
        raw_files=raw_files,
    )


# ---------------------------------------------------------------------------------
# Files: nested JSON + CSV
# ---------------------------------------------------------------------------------
def extract_driver_events(path: Path, bronze: Path, logger) -> Extracted:
    if not path.exists():
        raise SourceUnavailableError(f"Driver events export not found: {path}")
    out_dir = bronze / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    shutil.copy2(path, target)

    drivers = json.loads(path.read_text(encoding="utf-8"))
    flat = [{"driver_id": d["driver_id"], **e} for d in drivers for e in d.get("events", [])]
    df = pd.DataFrame(flat)
    declared = sum(len(d.get("events", [])) for d in drivers)
    logger.info("Extracted JSON | source=driver_events drivers=%s events=%s", len(drivers), len(df))
    return Extracted(
        name="driver_events", mode="JSON", system="Driver App", df=df,
        completeness={"drivers": len(drivers), "nested_events": declared, "flattened_rows": len(df),
                      "complete": declared == len(df)},
        raw_files=[_raw_ref(target, bronze)],
    )


def extract_csv(name: str, path: Path, system: str, bronze: Path, logger) -> Extracted:
    if not path.exists():
        raise SourceUnavailableError(f"{name} export not found: {path}")
    out_dir = bronze / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    shutil.copy2(path, target)

    with open(path, newline="", encoding="utf-8") as f:
        record_count = sum(1 for _ in csv.reader(f)) - 1          # records in the file, minus header
    df = pd.read_csv(path, dtype=str, keep_default_na=True)     # keep raw text; typing happens in CLEAN
    logger.info("Extracted CSV | source=%s rows=%s", name, len(df))
    return Extracted(
        name=name, mode="CSV", system=system, df=df,
        completeness={"file_records": record_count, "retrieved": len(df), "complete": record_count == len(df)},
        raw_files=[_raw_ref(target, bronze)],
    )
