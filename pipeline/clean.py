"""CLEAN — apply the agreed rules and the system-of-record decisions. Flag, never silently drop.

Silver layer = typed, de-duplicated, standardised tables + an issues log that records every
row a rule touched. Nothing here guesses a value an owner has not agreed to.

System-of-record (SoR) decisions applied here (reasoning in docs/01_source_map.md):
  * pickup time      -> Driver App `picked_up` event   (Order DB pickup_at is wrong for 5 orders and
                                                         filled in for cancelled orders)
  * delivery time    -> Order DB actual_delivery_at; Driver App `delivered` event ONLY as a flagged
                        fallback, and only when V12 proved the two agree
  * current driver   -> Dispatch API (Order DB keeps the original driver)
  * promised ETA     -> Order DB promised_eta (the customer promise); Dispatch current ETA is a
                        moving internal estimate
"""
from __future__ import annotations

import pandas as pd

from pipeline.common import map_categories, parse_ts, resolve_delivery_and_pickup


def _issue(rows, entity, ids, check_id, issue, action):
    for i in ids:
        rows.append({"entity": entity, "entity_id": i, "check_id": check_id, "issue": issue, "action": action})


def clean_all(extracted: dict, cfg, cmap, results, logger) -> dict:
    status = {r.check_id: r.status for r in results}
    recovery_enabled = status.get("V12") == "PASS"
    issues = []

    # ------------------------------------------------------------------ orders
    raw = extracted["orders"].df.copy()
    dup_ids = raw.loc[raw.duplicated("order_id", keep=False), "order_id"].unique()
    conflicts = {}
    for oid, g in raw[raw["order_id"].isin(dup_ids)].groupby("order_id"):
        cols = [c for c in raw.columns if g[c].nunique(dropna=False) > 1]
        conflicts[oid] = cols
        _issue(issues, "order", [oid], "V03", f"duplicate rows disagree on {cols or 'nothing'}",
               "kept first row")
    o = raw.drop_duplicates("order_id", keep="first").copy()
    logger.info("Cleaned orders | input_rows=%s output_rows=%s deduplicated=%s",
                len(raw), len(o), len(raw) - len(o))

    for c in ["created_at", "promised_eta", "pickup_at", "actual_delivery_at"]:
        o[c] = parse_ts(o[c])
    o["distance_km_estimate"] = pd.to_numeric(o["distance_km_estimate"], errors="coerce")
    for field in ["final_status", "traffic_bucket", "weather_bucket"]:
        o[field], _ = map_categories(o[field], field, cmap)

    life = resolve_delivery_and_pickup(o, extracted["driver_events"].df)
    o = o.join(life.rename(columns={"assigned": "app_assigned_at", "picked_up": "app_picked_up_at",
                                    "delivered": "app_delivered_at"}), on="order_id")

    cancelled = o["final_status"].eq("cancelled")
    delivered = o["final_status"].eq("delivered")

    # pickup: Driver App is SoR; DB value only if the app has nothing and the order was not cancelled
    o["pickup_at_resolved"] = o["app_picked_up_at"]
    use_db = o["pickup_at_resolved"].isna() & ~cancelled & o["pickup_at"].notna()
    o.loc[use_db, "pickup_at_resolved"] = o.loc[use_db, "pickup_at"]
    o["pickup_ts_source"] = "driver_app"
    o.loc[use_db, "pickup_ts_source"] = "order_db"
    o.loc[o["pickup_at_resolved"].isna(), "pickup_ts_source"] = "none"

    # delivery: DB is SoR; recover from Driver App only when proven equivalent (V12)
    o["delivered_at_resolved"] = o["actual_delivery_at"]
    o["delivery_ts_source"] = o["actual_delivery_at"].notna().map({True: "order_db", False: "none"})
    recover = delivered & o["actual_delivery_at"].isna() & o["app_delivered_at"].notna()
    if recovery_enabled:
        o.loc[recover, "delivered_at_resolved"] = o.loc[recover, "app_delivered_at"]
        o.loc[recover, "delivery_ts_source"] = "driver_app_recovered"
        _issue(issues, "order", o.loc[recover, "order_id"], "V11",
               "delivered but actual_delivery_at missing in Order DB", "recovered from Driver App delivered event")

    # flags (kept on every row so any consumer can filter or audit)
    o["flag_duplicate_conflict"] = o["order_id"].isin([k for k, v in conflicts.items() if v])
    o["flag_promise_invalid"] = o["promised_eta"] < o["created_at"]
    _issue(issues, "order", o.loc[o["flag_promise_invalid"], "order_id"], "V08",
           "promised_eta earlier than created_at", "excluded from KPI denominator")
    o["flag_db_pickup_after_delivery"] = o["pickup_at"] > o["actual_delivery_at"]
    _issue(issues, "order", o.loc[o["flag_db_pickup_after_delivery"], "order_id"], "V09",
           "Order DB pickup_at later than actual_delivery_at", "pickup taken from Driver App")
    o["flag_cancelled_with_db_pickup"] = cancelled & o["pickup_at"].notna() & o["app_picked_up_at"].isna()
    _issue(issues, "order", o.loc[o["flag_cancelled_with_db_pickup"], "order_id"], "V10",
           "cancelled order has a DB pickup_at but no Driver App pickup", "DB pickup ignored")
    sentinel = float(cfg.validation["distance_sentinel_km"])
    o["flag_distance_is_default"] = o["distance_km_estimate"].eq(sentinel)
    o["distance_km_measured"] = o["distance_km_estimate"].where(~o["flag_distance_is_default"])
    missing_unrecovered = delivered & o["delivered_at_resolved"].isna()
    _issue(issues, "order", o.loc[missing_unrecovered, "order_id"], "V11",
           "delivered with no completion timestamp in any source", "excluded from KPI denominator")
    for col in ["restaurant_id", "driver_id"]:
        _issue(issues, "order", o.loc[o[col].isna(), "order_id"], "V04", f"{col} is null",
               "kept; excluded from per-restaurant/per-driver cuts")

    # ------------------------------------------------------------------ dispatch
    d = extracted["dispatch"].df.copy()
    for c in ["assigned_at", "reassigned_at", "estimated_pickup_at", "current_delivery_eta"]:
        d[c] = parse_ts(d[c])
    d["reassigned"] = d["reassigned_at"].notna()
    d = d.rename(columns={"driver_id": "driver_id_current"})

    # ------------------------------------------------------------------ driver events (sorted by time)
    ev = extracted["driver_events"].df.copy()
    ev["timestamp"] = parse_ts(ev["timestamp"])
    ev = ev.sort_values(["order_id", "timestamp"]).reset_index(drop=True)

    # ------------------------------------------------------------------ support tickets
    t_raw = extracted["support_tickets"].df
    t = t_raw.drop_duplicates().copy()
    _issue(issues, "ticket", t_raw.loc[t_raw.duplicated(), "ticket_id"], "V21", "exact duplicate row", "removed")
    _issue(issues, "ticket", t.loc[t["order_id"].isna(), "ticket_id"], "V21", "no order_id",
           "kept, cannot be joined to an order")
    t["created_at"] = parse_ts(t["created_at"])
    t["category_raw"] = t["category"]
    t["category"], _ = map_categories(t["category"], "ticket_category", cmap)

    # ------------------------------------------------------------------ customer app actions
    a = extracted["customer_app_actions"].df.copy()
    a["action_at"] = parse_ts(a["action_at"])

    # ------------------------------------------------------------------ interventions
    iv = extracted["order_interventions"].df.copy()
    iv["intervention_at"] = parse_ts(iv["intervention_at"])
    ivx = iv.merge(o[["order_id", "created_at", "promised_eta", "delivered_at_resolved", "final_status"]],
                   on="order_id", how="left")
    iv["after_delivery"] = (ivx["intervention_at"] > ivx["delivered_at_resolved"]).values
    iv["on_cancelled_order"] = ivx["final_status"].eq("cancelled").values
    iv["preventive"] = ((ivx["intervention_at"] >= ivx["created_at"]) &
                        (ivx["intervention_at"] < ivx["promised_eta"]) &
                        (ivx["intervention_type"] != "REFUND_OFFER")).values   # a refund compensates, it cannot prevent
    _issue(issues, "intervention", iv.loc[iv["after_delivery"], "intervention_id"], "V14",
           "intervention after delivery", "not counted as preventive")

    # ------------------------------------------------------------------ restaurant status + restaurants
    rs_raw = extracted["restaurant_status"].df
    rs = rs_raw.drop_duplicates().copy()
    rs["status_raw"] = rs["status"]
    rs["status"], _ = map_categories(rs["status"], "restaurant_status", cmap)
    rs["last_updated_at"] = parse_ts(rs["last_updated_at"])

    rest = extracted["restaurants"].df.copy()
    bb = cfg.validation["bengaluru_bbox"]
    rest["coords_valid"] = rest["lat"].between(bb["lat_min"], bb["lat_max"]) & rest["lon"].between(bb["lon_min"], bb["lon_max"])
    _issue(issues, "restaurant", rest.loc[~rest["coords_valid"], "restaurant_id"], "V18",
           "coordinates outside Bengaluru", "flagged coords_valid=False")

    issues_df = pd.DataFrame(issues, columns=["entity", "entity_id", "check_id", "issue", "action"])
    logger.info("Clean complete | issues_logged=%s recovered_deliveries=%s recovery_enabled=%s",
                len(issues_df), int(recover.sum()) if recovery_enabled else 0, recovery_enabled)

    return {"orders": o, "dispatch": d, "driver_events": ev, "support_tickets": t,
            "customer_app_actions": a, "order_interventions": iv, "restaurant_status": rs,
            "restaurants": rest, "drivers": extracted["drivers"].df.copy(), "issues_log": issues_df}
