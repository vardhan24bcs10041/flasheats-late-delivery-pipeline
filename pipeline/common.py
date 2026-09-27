"""Small helpers shared by VALIDATE and CLEAN so both stages apply the same rule."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def parse_ts(series: pd.Series) -> pd.Series:
    """Sources mix `...T12:56:00` and `...T12:56:00.123456` — parse both, never crash on junk."""
    return pd.to_datetime(series, format="mixed", errors="coerce")


def minutes(delta: pd.Series) -> pd.Series:
    return delta.dt.total_seconds() / 60


def load_category_map(path: Path) -> pd.DataFrame:
    cmap = pd.read_csv(path, dtype=str, keep_default_na=False)
    cmap["owner_confirmed"] = cmap["owner_confirmed"].str.lower().eq("yes")
    return cmap


def map_categories(values: pd.Series, field: str, cmap: pd.DataFrame):
    """Map raw category values to canonical values using the agreed mapping file.

    Returns (mapped_series, report) where report lists representation fixes, semantic
    (owner-unconfirmed) mappings and unmapped values. Unmapped values are kept visible as
    `UNMAPPED:<raw>` — never guessed, never dropped.
    """
    rules = cmap[cmap["field"] == field]
    exact = dict(zip(rules["raw_value"], rules["canonical_value"]))
    confirmed = dict(zip(rules["raw_value"], rules["owner_confirmed"]))
    change = dict(zip(rules["raw_value"], rules["change_type"]))

    out, representation, semantic, unmapped = [], {}, {}, {}
    for v in values:
        if pd.isna(v):
            out.append(v)
            continue
        key = v if v in exact else None
        if key is None:   # tolerate whitespace/case drift the map has not seen yet
            for raw in exact:
                if raw.strip().lower() == str(v).strip().lower():
                    key = raw
                    break
        if key is None:
            out.append(f"UNMAPPED:{v}")
            unmapped[v] = unmapped.get(v, 0) + 1
            continue
        out.append(exact[key])
        if change.get(key) == "representation" or (key != v):
            representation[v] = representation.get(v, 0) + 1
        if change.get(key) == "semantic" and not confirmed.get(key, False):
            semantic[v] = semantic.get(v, 0) + 1
    return pd.Series(out, index=values.index), {
        "representation_fixes": representation,
        "unconfirmed_semantic": semantic,
        "unmapped": unmapped,
    }


def resolve_delivery_and_pickup(orders: pd.DataFrame, driver_events: pd.DataFrame) -> pd.DataFrame:
    """Per-order driver-app lifecycle timestamps (sorted by time, not by array order)."""
    ev = driver_events.copy()
    ev["ts"] = parse_ts(ev["timestamp"])
    ev = ev.sort_values(["order_id", "ts"])
    life = (ev[ev["type"].isin(["assigned", "picked_up", "delivered"])]
            .groupby(["order_id", "type"])["ts"].first().unstack())
    return life.reindex(columns=["assigned", "picked_up", "delivered"])
