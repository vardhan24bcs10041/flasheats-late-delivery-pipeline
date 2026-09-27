"""SAVE — idempotent, atomic publication by logical run date.

Every layer is written to a temporary folder first and swapped in only when complete:
the same --run-date always REPLACES its partition (never appends), and a crash mid-write
can never leave a half-published partition behind (Class 8).
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd


def temp_partition(base: Path, layer: str, run_date: str) -> Path:
    tmp = base / layer / f".tmp_run_date={run_date}_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def swap_in(tmp: Path, final: Path) -> Path:
    """Replace `final` with `tmp` (rename old aside, rename new in, delete old)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    old = None
    if final.exists():
        old = final.with_name(f".old_{final.name}_{uuid.uuid4().hex[:8]}")
        final.rename(old)
    tmp.rename(final)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return final


def discard(tmp: Path | None) -> None:
    if tmp is not None and tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S")


def write_json(obj, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def publish_layer(base: Path, layer: str, run_date: str, frames: dict, jsons: dict | None = None) -> Path:
    tmp = temp_partition(base, layer, run_date)
    try:
        for name, df in frames.items():
            _write_frame(df, tmp / f"{name}.csv")
        for name, obj in (jsons or {}).items():
            write_json(obj, tmp / f"{name}.json")
        return swap_in(tmp, base / layer / f"run_date={run_date}")
    except Exception:
        discard(tmp)
        raise
