"""METRICS — the 5 business metrics that link the workflow model to the project KPI.

  M1  Late Delivery Rate                      outcome        (lagging)  = the project KPI
  M2  Pickup SLA Miss Rate                    workflow       (leading, controllable by Ops/Dispatch)
  M3  Customer Escalation Rate on late orders customer       (leading signal of CX damage)
  M4  Preventive Intervention Coverage        intervention   (controllable)
  M5  ETA Drift                               ETA honesty    (diagnostic)

Definitions (formula, grain, owner) are documented in docs/04_metrics.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _r(x, n=1):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def compute_metrics(j: pd.DataFrame, s: dict, cfg) -> tuple[dict, dict]:
    k = cfg.kpi
    e = j[j["kpi_eligible"]].copy()
    late = e[e["is_late"] == 1]
    ontime = e[e["is_late"] == 0]

    # ---- M1 -------------------------------------------------------------------------------------
    m1 = {
        "name": "Late Delivery Rate", "type": "Outcome (lagging) — project KPI",
        "value_pct": _r(100 * e["is_late"].mean()), "numerator": int(len(late)), "denominator": int(len(e)),
        "median_lateness_of_late_min": _r(late["delay_min"].median()),
        "severe_late_rate_pct_gt10": _r(100 * e["is_late_10"].mean()),
        "p90_delay_min": _r(e["delay_min"].quantile(0.9)),
    }

    # KPI under every stakeholder definition (Class 6: the number depends on the definition)
    o = s["orders"]
    hist = o[o["final_status"].eq("delivered") & o["actual_delivery_at"].notna()]
    hist_delay = (hist["actual_delivery_at"] - hist["promised_eta"]).dt.total_seconds() / 60
    kpi_defs = pd.DataFrame([
        {"definition": "VP Operations — any delivered order after promised ETA  [PUBLISHED]",
         "population": "delivered, completion time from any agreed source, valid promise",
         "orders": len(e), "late_rate_pct": _r(100 * e["is_late"].mean())},
        {"definition": "Support Lead — only > 10 min late counts",
         "population": "same as above", "orders": len(e), "late_rate_pct": _r(100 * e["is_late_10"].mean())},
        {"definition": "Finance — exclude cancelled/refunded",
         "population": "same as VP Ops (cancelled already excluded)", "orders": len(e),
         "late_rate_pct": _r(100 * e["is_late"].mean())},
        {"definition": "Data Team (historical dashboard) — delivered with non-null DB actual time",
         "population": "Order DB only, no recovery, no promise check", "orders": len(hist),
         "late_rate_pct": _r(100 * (hist_delay > 0).mean())},
    ])

    # ---- M2 + stage decomposition ---------------------------------------------------------------
    miss = e[e["pickup_sla_miss"] == 1]
    hit = e[e["pickup_sla_miss"] == 0]
    m2 = {
        "name": "Pickup SLA Miss Rate", "type": "Workflow driver (leading, controllable)",
        "value_pct": _r(100 * e["pickup_sla_miss"].mean()),
        "definition": f"picked up > {k['pickup_sla_grace_min']} min after Dispatch's estimated_pickup_at",
        "late_rate_if_miss_pct": _r(100 * miss["is_late"].mean()),
        "late_rate_if_on_time_pickup_pct": _r(100 * hit["is_late"].mean()),
        "late_orders_explained_by_pickup_pct": _r(100 * (late["pickup_overrun_min"] >= late["delay_min"]).mean()),
    }
    stages = ["assign_lag_min", "first_mile_min", "pickup_overrun_min", "transit_min", "delay_min"]
    stage_tbl = pd.DataFrame({
        "stage": ["Order -> driver assigned", "Assigned -> picked up (first mile)",
                  "Pickup vs Dispatch's own estimate", "Picked up -> delivered (transit)", "Delivered vs promise"],
        "on_time_median_min": [_r(ontime[c].median()) for c in stages],
        "late_median_min": [_r(late[c].median()) for c in stages],
    })
    stage_tbl["gap_min"] = (stage_tbl["late_median_min"] - stage_tbl["on_time_median_min"]).round(1)

    # ---- M3 -------------------------------------------------------------------------------------
    sev = e[e["is_late_10"] == 1]
    m3 = {
        "name": "Customer Escalation Rate (late orders)", "type": "Customer interaction (leading CX signal)",
        "value_pct": _r(100 * late["customer_escalated"].mean()),
        "definition": "late orders where the customer opened support in-app OR raised a ticket / late orders",
        "severe_late_escalation_pct": _r(100 * sev["customer_escalated"].mean()),
        "on_time_escalation_pct": _r(100 * ontime["customer_escalated"].mean()),
        "late_orders_with_ticket_pct": _r(100 * (late["ticket_count"] > 0).mean()),
        "support_opened_orders_that_were_late_pct": _r(100 * e.loc[e["support_opened_count"] > 0, "is_late"].mean()),
    }

    # ---- M4 -------------------------------------------------------------------------------------
    iv = s["order_interventions"].merge(e[["order_id", "is_late"]], on="order_id", how="inner")
    by_type = (iv.groupby("intervention_type")
               .agg(interventions=("intervention_id", "count"), preventive=("preventive", "sum"),
                    late_rate_pct=("is_late", lambda x: round(100 * x.mean(), 1)))
               .reset_index().sort_values("interventions", ascending=False))
    with_prev = e[e["preventive_intervention"]]
    none = e[e["intervention_count"] == 0]
    m4 = {
        "name": "Preventive Intervention Coverage (late orders)", "type": "Intervention (controllable)",
        "value_pct": _r(100 * late["preventive_intervention"].mean()),
        "definition": "late orders that got a non-refund intervention between order creation and promised ETA / late orders",
        "late_rate_with_preventive_pct": _r(100 * with_prev["is_late"].mean()),
        "late_rate_no_intervention_pct": _r(100 * none["is_late"].mean()),
        "note": "association only — interventions target at-risk orders (selection bias); no control group",
    }

    # ---- M5 -------------------------------------------------------------------------------------
    revised_late = (e["delivered_at"] > e["current_delivery_eta"]).mean()
    m5 = {
        "name": "ETA Drift", "type": "ETA reliability (diagnostic)",
        "value_min": _r(e["eta_drift_min"].median()),
        "definition": "median(Dispatch current_delivery_eta - promised_eta)",
        "late_vs_revised_eta_pct": _r(100 * revised_late),
        "late_vs_original_promise_pct": m1["value_pct"],
    }

    # ---- trend + context segments ---------------------------------------------------------------
    e["week"] = e["created_at"].dt.strftime("%G-W%V")
    weekly = (e.groupby("week").agg(orders=("order_id", "count"),
                                    late_rate_pct=("is_late", lambda x: round(100 * x.mean(), 1)),
                                    pickup_sla_miss_pct=("pickup_sla_miss", lambda x: round(100 * x.mean(), 1)))
              .reset_index())
    seg = []
    for dim in ["traffic_bucket", "weather_bucket"]:
        g = e.groupby(dim).agg(orders=("order_id", "count"), late_rate_pct=("is_late", lambda x: round(100 * x.mean(), 1)),
                               median_first_mile=("first_mile_min", "median"), median_transit=("transit_min", "median"))
        g = g.reset_index().rename(columns={dim: "value"}).assign(dimension=dim)
        seg.append(g)
    segments = pd.concat(seg)[["dimension", "value", "orders", "late_rate_pct", "median_first_mile", "median_transit"]].round(1)

    dq = {
        "orders_total": int(len(j)),
        "kpi_eligible": int(len(e)),
        "excluded_cancelled": int(j["final_status"].eq("cancelled").sum()),
        "excluded_promise_invalid": int((j["final_status"].eq("delivered") & j["dq_flags"].str.contains("promise_invalid")).sum()),
        "excluded_no_completion_time": int((j["final_status"].eq("delivered") & j["delivered_at"].isna()).sum()),
        "recovered_from_driver_app": int(j["delivery_ts_source"].eq("driver_app_recovered").sum()),
        "recovered_orders_late_rate_pct": _r(100 * e.loc[e["delivery_ts_source"].eq("driver_app_recovered"), "is_late"].mean()),
    }

    metrics = {"M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5, "population": dq}
    tables = {"kpi_by_definition": kpi_defs, "stage_decomposition": stage_tbl, "intervention_by_type": by_type,
              "weekly_trend": weekly, "segments": segments}
    return metrics, tables
