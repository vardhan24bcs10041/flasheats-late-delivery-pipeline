"""TRANSFORM — reorganise source-shaped tables around the business workflow (Class 7).

Gold tables:
  order_journey   one row per order: lifecycle stages, outcome, customer reaction, interventions
  order_timeline  one row per event, from every source, in time order (entity -> events -> state)

Rule used everywhere: aggregate one-to-many tables to the order grain BEFORE joining.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.common import minutes


def build_order_journey(s: dict, cfg, logger) -> pd.DataFrame:
    o, d = s["orders"], s["dispatch"]
    k = cfg.kpi

    j = o.merge(d[["order_id", "driver_id_current", "original_driver_id", "assigned_at", "reassigned",
                   "reassigned_at", "estimated_pickup_at", "current_delivery_eta", "dispatch_status",
                   "eta_model_version"]], on="order_id", how="left")
    j = j.rename(columns={"driver_id": "driver_id_order_db", "pickup_at_resolved": "picked_up_at",
                          "delivered_at_resolved": "delivered_at"})

    # ---- lifecycle stages (where does time go?) ---------------------------------------------
    j["assign_lag_min"] = minutes(j["assigned_at"] - j["created_at"])            # order -> driver assigned
    j["first_mile_min"] = minutes(j["picked_up_at"] - j["assigned_at"])          # assigned -> picked up
    j["pickup_overrun_min"] = minutes(j["picked_up_at"] - j["estimated_pickup_at"])  # vs dispatch's own plan
    j["transit_min"] = minutes(j["delivered_at"] - j["picked_up_at"])            # picked up -> delivered
    j["delay_min"] = minutes(j["delivered_at"] - j["promised_eta"])              # outcome vs promise
    j["eta_drift_min"] = minutes(j["current_delivery_eta"] - j["promised_eta"])  # how far the ETA moved

    # ---- outcome ---------------------------------------------------------------------------------
    j["kpi_eligible"] = (j["final_status"].eq("delivered") & j["delivered_at"].notna()
                         & j["promised_eta"].notna() & ~j["flag_promise_invalid"])
    j["is_late"] = np.where(j["kpi_eligible"], j["delay_min"] > k["late_threshold_min"], np.nan)
    j["is_late_10"] = np.where(j["kpi_eligible"], j["delay_min"] > k["severe_threshold_min"], np.nan)
    j["pickup_sla_miss"] = np.where(j["kpi_eligible"], j["pickup_overrun_min"] > k["pickup_sla_grace_min"], np.nan)
    j["outcome"] = np.select(
        [j["final_status"].eq("cancelled"), ~j["kpi_eligible"], j["is_late"] == 1],
        ["cancelled", "not_measurable", "delivered_late"], default="delivered_on_time")

    # ---- customer interactions (aggregate first, then join) --------------------------------------
    a = s["customer_app_actions"]
    acts = (a.pivot_table(index="order_id", columns="action_type", values="action_id", aggfunc="count", fill_value=0)
            .rename(columns={"ETA_VIEWED": "eta_views", "SUPPORT_OPENED": "support_opened_count",
                             "CANCEL_ATTEMPTED": "cancel_attempted_count"}))
    t = s["support_tickets"].dropna(subset=["order_id"])
    tick = t.groupby("order_id").agg(ticket_count=("ticket_id", "count"),
                                     ticket_categories=("category", lambda x: ",".join(sorted(set(x)))),
                                     first_ticket_at=("created_at", "min"))
    iv = s["order_interventions"]
    ivs = iv.groupby("order_id").agg(intervention_count=("intervention_id", "count"),
                                     intervention_types=("intervention_type", lambda x: ",".join(sorted(set(x)))),
                                     preventive_intervention=("preventive", "any"),
                                     first_intervention_at=("intervention_at", "min"))
    j = j.join(acts, on="order_id").join(tick, on="order_id").join(ivs, on="order_id")
    for c in ["eta_views", "support_opened_count", "cancel_attempted_count", "ticket_count", "intervention_count"]:
        j[c] = j[c].fillna(0).astype(int) if c in j else 0
    j["preventive_intervention"] = j["preventive_intervention"].fillna(False).astype(bool)
    j["customer_escalated"] = (j["support_opened_count"] > 0) | (j["ticket_count"] > 0)

    flag_cols = [c for c in j.columns if c.startswith("flag_")]
    j["dq_flags"] = j[flag_cols].apply(lambda r: ";".join(c[5:] for c in flag_cols if r[c]), axis=1)

    cols = ["order_id", "customer_id", "restaurant_id", "driver_id_order_db", "driver_id_current", "reassigned",
            "created_at", "promised_eta", "assigned_at", "estimated_pickup_at", "picked_up_at", "delivered_at",
            "current_delivery_eta", "final_status", "outcome", "kpi_eligible", "is_late", "is_late_10",
            "delay_min", "assign_lag_min", "first_mile_min", "pickup_overrun_min", "pickup_sla_miss", "transit_min",
            "eta_drift_min", "traffic_bucket", "weather_bucket", "distance_km_measured", "eta_model_version",
            "eta_views", "support_opened_count", "cancel_attempted_count", "ticket_count", "ticket_categories",
            "customer_escalated", "intervention_count", "intervention_types", "preventive_intervention",
            "pickup_ts_source", "delivery_ts_source", "dq_flags"]
    j = j[cols].sort_values("order_id").reset_index(drop=True)
    logger.info("Built order_journey | rows=%s kpi_eligible=%s columns=%s", len(j), int(j["kpi_eligible"].sum()), len(cols))
    return j


def build_order_timeline(s: dict, logger) -> pd.DataFrame:
    """Event model: every state change for an order, from whichever system observed it."""
    o, d = s["orders"], s["dispatch"]
    parts = [
        o[["order_id", "created_at", "customer_id"]].rename(columns={"created_at": "event_time", "customer_id": "actor_id"})
            .assign(event_type="ORDER_CREATED", actor_type="customer", source_system="order_db"),
        d[["order_id", "assigned_at", "original_driver_id"]].rename(columns={"assigned_at": "event_time", "original_driver_id": "actor_id"})
            .assign(event_type="DRIVER_ASSIGNED", actor_type="dispatch", source_system="dispatch_api"),
        d.loc[d["reassigned"], ["order_id", "reassigned_at", "driver_id_current"]]
            .rename(columns={"reassigned_at": "event_time", "driver_id_current": "actor_id"})
            .assign(event_type="DRIVER_REASSIGNED", actor_type="dispatch", source_system="dispatch_api"),
    ]
    ev = s["driver_events"]
    ev = ev[ev["type"].isin(["picked_up", "delivered"])]
    parts.append(ev.rename(columns={"timestamp": "event_time", "driver_id": "actor_id"})
                 .assign(event_type=lambda x: x["type"].str.upper(), actor_type="driver", source_system="driver_app")
                 [["order_id", "event_time", "actor_id", "event_type", "actor_type", "source_system"]])
    a = s["customer_app_actions"]
    parts.append(a.rename(columns={"action_at": "event_time", "customer_id": "actor_id", "action_type": "event_type"})
                 .assign(actor_type="customer", source_system="customer_app")
                 [["order_id", "event_time", "actor_id", "event_type", "actor_type", "source_system"]])
    t = s["support_tickets"].dropna(subset=["order_id"])
    parts.append(t.rename(columns={"created_at": "event_time", "ticket_id": "actor_id"})
                 .assign(event_type=lambda x: "TICKET_" + x["category"].str.upper(), actor_type="customer",
                         source_system="support_platform")
                 [["order_id", "event_time", "actor_id", "event_type", "actor_type", "source_system"]])
    iv = s["order_interventions"]
    parts.append(iv.rename(columns={"intervention_at": "event_time", "initiated_by": "actor_id"})
                 .assign(event_type=lambda x: "INTERVENTION_" + x["intervention_type"], actor_type="operations",
                         source_system="ops_intervention_log")
                 [["order_id", "event_time", "actor_id", "event_type", "actor_type", "source_system"]])
    rs = s["restaurant_status"]
    parts.append(rs.rename(columns={"last_updated_at": "event_time", "restaurant_id": "actor_id"})
                 .assign(event_type=lambda x: "RESTAURANT_STATUS_" + x["status"].str.upper(), actor_type="restaurant",
                         source_system="restaurant_status_feed")
                 [["order_id", "event_time", "actor_id", "event_type", "actor_type", "source_system"]])

    tl = pd.concat(parts, ignore_index=True)
    tl = tl.sort_values(["order_id", "event_time", "event_type"]).reset_index(drop=True)
    tl = tl[["order_id", "event_time", "event_type", "actor_type", "actor_id", "source_system"]]
    logger.info("Built order_timeline | events=%s orders=%s sources=%s", len(tl), tl["order_id"].nunique(),
                tl["source_system"].nunique())
    return tl
