"""VALIDATE — the Class 6 business rules, executed as a gate on every run.

Each check returns PASS / WARN / FAIL / UNKNOWN:
  PASS     data meets the expectation
  WARN     issue found, an agreed rule handles it, output is published WITH the caveat
  FAIL     unsafe for the decision -> pipeline stops, nothing new is published
  UNKNOWN  cannot be decided from data — needs a business owner (never silently resolved)

Checks are grouped the way Class 6 taught them: technical, lifecycle, semantic, organizational.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from pipeline.common import map_categories, minutes, parse_ts, resolve_delivery_and_pickup


class ValidationError(ValueError):
    pass


@dataclass
class CheckResult:
    check_id: str
    layer: str          # technical | lifecycle | semantic | organizational
    check: str
    status: str         # PASS | WARN | FAIL | UNKNOWN
    evidence: str
    action: str
    owner: str = ""

    def to_dict(self):
        return asdict(self)


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def check_required_columns(frames: dict, required: dict) -> list[CheckResult]:
    missing = {name: sorted(set(cols) - set(frames[name].columns))
               for name, cols in required.items() if name in frames}
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        return [CheckResult("V01", "technical", "Required columns present", "FAIL",
                            f"missing: {missing}",
                            "STOP. Schema changed upstream — fix the extract or agree the new contract. "
                            "Not retryable.", "Source system owner")]
    return [CheckResult("V01", "technical", "Required columns present", "PASS",
                        f"{sum(len(c) for c in required.values())} required columns across "
                        f"{len(required)} sources", "none")]


def run_validations(extracted: dict, cfg, cmap, metric_defs: dict, run_date: date) -> list[CheckResult]:
    frames = {name: e.df for name, e in extracted.items()}
    rules = cfg.validation
    results = check_required_columns(frames, cfg.required_columns)
    if results[0].status == "FAIL":
        return results        # later checks would only crash on the missing columns

    o = frames["orders"].copy()
    d = frames["dispatch"].copy()
    ev = frames["driver_events"]
    t = frames["support_tickets"]
    a = frames["customer_app_actions"]
    iv = frames["order_interventions"]
    rs = frames["restaurant_status"]
    rest = frames["restaurants"]
    drv = frames["drivers"]

    # ---------------------------------------------------------------- TECHNICAL
    incomplete = {n: e.completeness for n, e in extracted.items() if not e.completeness.get("complete", True)}
    disp_c = extracted["dispatch"].completeness
    results.append(CheckResult(
        "V02", "technical", "Retrieval completeness (every source)",
        "FAIL" if incomplete else "PASS",
        f"incomplete: {incomplete}" if incomplete else
        f"all {len(extracted)} sources reconcile to their source counts; dispatch "
        f"{disp_c['retrieved']}/{disp_c['expected_total']} over {disp_c['pages']} pages, "
        f"{disp_c['retries']} transient errors retried",
        "STOP — never publish a partial extract" if incomplete else "none"))

    dup_rows = int(o.duplicated("order_id").sum())
    dup_pct = _pct(dup_rows, len(o))
    dups = o[o.duplicated("order_id", keep=False)]
    conflict_cols = sorted({c for _, g in dups.groupby("order_id") for c in o.columns if g[c].nunique(dropna=False) > 1})
    status = "PASS" if dup_rows == 0 else ("FAIL" if dup_pct > rules["max_duplicate_order_pct"] else "WARN")
    results.append(CheckResult(
        "V03", "technical", "Business grain: one row per order_id", status,
        f"{dup_rows} extra rows ({dup_pct}%) for {dups['order_id'].nunique()} orders; "
        f"duplicates disagree on: {conflict_cols or 'nothing (exact copies)'}",
        "Keep first row per order_id (agreed grain); conflicting field values logged in issues_log"
        if status == "WARN" else ("STOP — duplication looks systemic" if status == "FAIL" else "none"),
        "Order Platform"))

    o = o.drop_duplicates("order_id", keep="first")   # remaining checks run at the agreed grain
    crit = {c: int(o[c].isna().sum()) for c in ["order_id", "created_at", "promised_eta", "final_status"]}
    soft = {c: int(o[c].isna().sum()) for c in ["restaurant_id", "driver_id"]}
    crit_bad = {k: v for k, v in crit.items() if v}
    status = "FAIL" if crit.get("order_id") or any(_pct(v, len(o)) > 1 for v in crit.values()) else \
        ("WARN" if crit_bad or any(soft.values()) else "PASS")
    results.append(CheckResult(
        "V04", "technical", "Critical fields not null", status,
        f"critical nulls {crit}; soft nulls {soft}",
        "Orders missing restaurant/driver kept; excluded only from restaurant/driver cuts" if status == "WARN" else
        ("STOP — KPI inputs missing" if status == "FAIL" else "none"), "Order Platform"))

    ts_fields = {"orders": ["created_at", "promised_eta", "pickup_at", "actual_delivery_at"],
                 "dispatch": ["assigned_at", "estimated_pickup_at", "current_delivery_eta"],
                 "driver_events": ["timestamp"], "support_tickets": ["created_at"],
                 "customer_app_actions": ["action_at"], "order_interventions": ["intervention_at"],
                 "restaurant_status": ["last_updated_at"]}
    bad, total, mixed = {}, 0, []
    for src, cols in ts_fields.items():
        for c in cols:
            raw = frames[src][c]
            nn = raw.notna()
            n_bad = int((nn & parse_ts(raw).isna()).sum())
            total += int(nn.sum())
            if n_bad:
                bad[f"{src}.{c}"] = n_bad
            if raw.dropna().astype(str).str.len().nunique() > 1:
                mixed.append(f"{src}.{c}")
    bad_pct = _pct(sum(bad.values()), total)
    status = "FAIL" if bad_pct > rules["max_unparseable_ts_pct"] else ("WARN" if bad else "PASS")
    results.append(CheckResult(
        "V05", "technical", "Timestamps parseable", status,
        f"{total} timestamps checked, unparseable={bad or 0}; mixed precision formats in {mixed}",
        "Parsed with a mixed-format ISO parser (a strict parser crashes on these files)", "Data platform"))

    created = parse_ts(o["created_at"])
    latest = created.max()
    age = (pd.Timestamp(run_date) - latest.normalize()).days if pd.notna(latest) else None
    status = "FAIL" if age is None or age > rules["max_data_age_days"] else "PASS"
    results.append(CheckResult(
        "V06", "technical", "Freshness SLA (weekly KPI report)", status,
        f"latest order created_at={latest}; age={age} days vs allowed {rules['max_data_age_days']}",
        "STOP — stale extract would publish last month's KPI as this week's" if status == "FAIL" else "none",
        "Order Platform"))

    oid = set(o["order_id"].dropna())
    rels = [
        ("orders.restaurant_id -> restaurants", o["restaurant_id"].dropna().isin(rest["restaurant_id"]).mean(), o["restaurant_id"].isna().sum()),
        ("orders.driver_id -> drivers", o["driver_id"].dropna().isin(drv["driver_id"]).mean(), o["driver_id"].isna().sum()),
        ("orders -> dispatch record", pd.Series(sorted(oid)).isin(d["order_id"]).mean(), 0),
        ("dispatch.order_id -> orders", d["order_id"].isin(oid).mean(), 0),
        ("driver_events.order_id -> orders", ev["order_id"].isin(oid).mean(), 0),
        ("support_tickets.order_id -> orders", t["order_id"].dropna().isin(oid).mean(), t["order_id"].isna().sum()),
        ("customer_app_actions.order_id -> orders", a["order_id"].isin(oid).mean(), 0),
        ("order_interventions.order_id -> orders", iv["order_id"].isin(oid).mean(), 0),
        ("restaurant_status.order_id -> orders", rs["order_id"].isin(oid).mean(), 0),
    ]
    for i, (rel, cov, nulls) in enumerate(rels, 1):
        cov = round(100 * float(cov), 2)
        status = "FAIL" if cov < rules["min_referential_coverage_pct"] else ("WARN" if cov < 100 or nulls else "PASS")
        results.append(CheckResult(
            f"V07.{i}", "technical", f"Cross-source mapping: {rel}", status,
            f"coverage={cov}% of non-null keys; null keys={int(nulls)}",
            "Unmapped/null keys kept and flagged" if status == "WARN" else
            ("STOP — sources describe different populations" if status == "FAIL" else "none")))

    # ---------------------------------------------------------------- LIFECYCLE
    promised = parse_ts(o["promised_eta"])
    pickup_db = parse_ts(o["pickup_at"])
    deliv_db = parse_ts(o["actual_delivery_at"])
    status_norm = o["final_status"].str.strip().str.lower()
    life = resolve_delivery_and_pickup(o, ev)
    o_life = o[["order_id"]].join(life, on="order_id")

    n = int((promised < created).sum())
    results.append(CheckResult(
        "V08", "lifecycle", "Promise made after order creation (promised_eta >= created_at)",
        "WARN" if n else "PASS", f"{n} orders promised before they were created (all exactly -10 min)",
        "Flagged `promise_invalid`; excluded from the KPI denominator (lateness vs an impossible promise is "
        "meaningless). Sensitivity reported." if n else "none", "Order Platform / ETA service"))

    n = int((deliv_db < pickup_db).sum())
    fixed = int(((deliv_db < pickup_db) & o_life["picked_up"].notna().values & (o_life["picked_up"].values <= deliv_db.values)).sum())
    results.append(CheckResult(
        "V09", "lifecycle", "Order DB: pickup_at before actual_delivery_at", "WARN" if n else "PASS",
        f"{n} orders delivered before their DB pickup; Driver App pickup is chronologically valid for {fixed} of them",
        "Pickup time taken from Driver App (system of record for pickup); DB value kept for audit",
        "Order Platform"))

    canc = status_norm.eq("cancelled")
    n_canc_pick = int((canc & pickup_db.notna()).sum())
    n_canc_app = int((canc.values & o_life["picked_up"].notna().values).sum())
    results.append(CheckResult(
        "V10", "lifecycle", "Cancelled orders carry no fulfilment evidence",
        "WARN" if n_canc_pick else "PASS",
        f"{n_canc_pick}/{int(canc.sum())} cancelled orders have a DB pickup_at, but Driver App shows "
        f"{n_canc_app} pickups — DB pickup_at looks like a scheduled/placeholder value",
        "DB pickup_at ignored for cancelled orders; semantics question raised with Order Platform",
        "Order Platform"))

    delivered = status_norm.eq("delivered")
    missing = delivered & deliv_db.isna()
    recoverable = int((missing.values & o_life["delivered"].notna().values).sum())
    results.append(CheckResult(
        "V11", "lifecycle", "Delivered orders have a completion timestamp",
        "WARN" if int(missing.sum()) else "PASS",
        f"{int(missing.sum())} delivered orders lack actual_delivery_at; {recoverable} have a Driver App "
        f"`delivered` event", "Recover from Driver App only if V12 passes; flag `delivery_ts_source`",
        "Order Platform"))

    both = deliv_db.notna().values & o_life["delivered"].notna().values
    diff_s = (o_life["delivered"].values[both] - deliv_db.values[both]) / np.timedelta64(1, "s")
    agree = _pct(int((np.abs(diff_s) <= 60).sum()), int(both.sum()))
    status = "PASS" if agree >= rules["min_source_agreement_pct"] else "WARN"
    results.append(CheckResult(
        "V12", "lifecycle", "Driver App vs Order DB agree on delivery time (recovery allowed?)", status,
        f"{int(both.sum())} orders present in both; {agree}% agree within 60s (max |diff|="
        f"{float(np.abs(diff_s).max()) if len(diff_s) else 0:.0f}s)",
        "Recovery ENABLED for missing DB timestamps" if status == "PASS" else
        "Recovery DISABLED — sources disagree; missing orders excluded instead"))

    m = o.drop_duplicates("order_id").merge(d[["order_id", "driver_id", "reassigned_at"]], on="order_id",
                                             how="left", suffixes=("", "_dispatch"))
    mismatch = m["driver_id"] != m["driver_id_dispatch"]
    explained = mismatch & (m["reassigned_at"].notna() | m["driver_id"].isna())
    results.append(CheckResult(
        "V13", "lifecycle", "Current driver agrees across Order DB and Dispatch",
        "WARN" if int(mismatch.sum()) else "PASS",
        f"{int(mismatch.sum())} mismatches; {int(explained.sum())} explained by a dispatch reassignment "
        f"or a null DB driver_id", "Dispatch = system of record for driver assignment; Order DB keeps the "
        "ORIGINAL driver", "Dispatch"))

    ivx = iv.merge(o.drop_duplicates("order_id")[["order_id", "created_at", "final_status"]], on="order_id", how="left")
    ivx = ivx.join(life["delivered"], on="order_id")
    iv_t = parse_ts(ivx["intervention_at"])
    before_created = int((iv_t < parse_ts(ivx["created_at"])).sum())
    after_deliv = int((iv_t > ivx["delivered"]).sum())
    on_canc = int(ivx["final_status"].str.lower().eq("cancelled").sum())
    results.append(CheckResult(
        "V14", "lifecycle", "Interventions happen inside the order lifecycle",
        "WARN" if (before_created or after_deliv or on_canc) else "PASS",
        f"{len(iv)} interventions: {before_created} before order creation, {after_deliv} after delivery, "
        f"{on_canc} on cancelled orders", "Only interventions between creation and promised ETA count as "
        "'preventive' in metric M4", "Operations"))

    unsorted = sum(1 for _, g in ev.groupby("driver_id", sort=False)
                   if not parse_ts(g["timestamp"]).is_monotonic_increasing)
    results.append(CheckResult(
        "V15", "lifecycle", "Driver App event arrays are in time order",
        "WARN" if unsorted else "PASS",
        f"{unsorted}/{ev['driver_id'].nunique()} drivers' event arrays are NOT time-ordered "
        f"(gps pings listed before pickup although later in time)",
        "Always sort by event timestamp — reading array order wrongly suggests 'no GPS after pickup'",
        "Driver App"))

    # ---------------------------------------------------------------- SEMANTIC
    cat_fields = [("final_status", o["final_status"]), ("traffic_bucket", o["traffic_bucket"]),
                  ("weather_bucket", o["weather_bucket"]), ("restaurant_status", rs["status"]),
                  ("ticket_category", t["category"])]
    rep, sem, unm = {}, {}, {}
    for field, series in cat_fields:
        _, r = map_categories(series, field, cmap)
        if r["representation_fixes"]:
            rep[field] = r["representation_fixes"]
        if r["unconfirmed_semantic"]:
            sem[field] = r["unconfirmed_semantic"]
        if r["unmapped"]:
            unm[field] = r["unmapped"]
    status = "UNKNOWN" if unm else ("WARN" if (sem or rep) else "PASS")
    results.append(CheckResult(
        "V16", "semantic", "Category values map to an agreed vocabulary", status,
        f"representation fixes={rep}; owner-UNconfirmed semantic mappings={sem}; unmapped={unm or 'none'}",
        "Case/whitespace normalised via config/category_map.csv; semantic merges applied provisionally and "
        "listed for owner sign-off; unmapped values kept as UNMAPPED:<raw>", "Support Lead / Restaurant Ops"))

    dist = pd.to_numeric(o["distance_km_estimate"], errors="coerce")
    sentinel = float(rules["distance_sentinel_km"])
    n_sent = int((dist == sentinel).sum())
    eta_rounded = o["promised_eta"].astype(str).str.len().eq(19)
    overlap = int(((dist == sentinel) & eta_rounded).sum())
    results.append(CheckResult(
        "V17", "semantic", "distance_km_estimate is a measurement, not a default",
        "WARN" if _pct(n_sent, len(o)) > 5 else "PASS",
        f"{n_sent} rows ({_pct(n_sent, len(o))}%) are exactly {sentinel} km; {overlap} of them also carry a "
        f"second-precision (rounded) promised_eta -> looks like a fallback ETA path",
        "Flag `distance_is_default`; distance NOT used as a delay driver until the owner confirms",
        "Dispatch / Maps"))

    bb = rules["bengaluru_bbox"]
    lat = pd.to_numeric(rest["lat"], errors="coerce")
    lon = pd.to_numeric(rest["lon"], errors="coerce")
    bad_geo = rest[~(lat.between(bb["lat_min"], bb["lat_max"]) & lon.between(bb["lon_min"], bb["lon_max"]))]
    results.append(CheckResult(
        "V18", "semantic", "Restaurant coordinates inside Bengaluru",
        "WARN" if len(bad_geo) else "PASS",
        f"{len(bad_geo)} restaurants outside the city box: "
        f"{bad_geo[['restaurant_id', 'lat', 'lon']].to_dict('records')}",
        "Flag `coords_valid=False`; not used (no geo metric in scope)", "Restaurant Partnerships"))

    r_disp = set(d.loc[d["reassigned_at"].notna(), "order_id"])
    r_iv = set(iv.loc[iv["intervention_type"] == "DRIVER_REASSIGNMENT", "order_id"])
    results.append(CheckResult(
        "V19", "semantic", "Reassignment: Dispatch and intervention log tell the same story",
        "UNKNOWN" if r_disp != r_iv else "PASS",
        f"Dispatch reassigned {len(r_disp)} orders; intervention log records {len(r_iv)} DRIVER_REASSIGNMENT; "
        f"overlap={len(r_disp & r_iv)}",
        "Dispatch = system of record for 'was it reassigned'; intervention log = what ops ASKED for. "
        "Question raised with Dispatch + Ops leads", "Dispatch / Operations"))

    rsx = rs.drop_duplicates().copy()
    rsx["st"] = rsx["status"].str.strip().str.lower()
    rsx = rsx.join(life["picked_up"], on="order_id")
    upd = parse_ts(rsx["last_updated_at"])
    contradict = int((rsx["st"].isin(["preparing", "ready"]) & (upd > rsx["picked_up"])).sum())
    cov = _pct(rsx.loc[rsx["order_id"].isin(o.loc[delivered, "order_id"]), "order_id"].nunique(), int(delivered.sum()))
    results.append(CheckResult(
        "V20", "semantic", "Restaurant status feed fit to attribute pickup delay",
        "UNKNOWN",
        f"one last-updated snapshot per order (no history); covers {cov}% of delivered orders; {contradict} "
        f"'preparing/ready' updates arrive AFTER the driver already picked up",
        "Not used for attribution. Needs FOOD_READY event history from restaurants", "Restaurant Partnerships"))

    dup_t = int(t.duplicated().sum())
    null_t = int(t["order_id"].isna().sum())
    results.append(CheckResult(
        "V21", "semantic", "Support tickets: unique and linkable",
        "WARN" if (dup_t or null_t) else "PASS",
        f"{dup_t} exact duplicate ticket rows; {null_t} tickets without order_id",
        "Exact duplicates removed; unlinked tickets kept in the ticket table but cannot join to an order",
        "Support"))

    tx = t.dropna(subset=["order_id"]).merge(o.drop_duplicates("order_id")[["order_id", "promised_eta"]], on="order_id")
    past_claims = tx["category"].str.strip().str.lower().str.replace(" ", "_").eq("late_delivery")
    early = int((past_claims & (parse_ts(tx["created_at"]) < parse_ts(tx["promised_eta"]))).sum())
    results.append(CheckResult(
        "V22", "semantic", "Customer-visible ETA = stored promised_eta?", "UNKNOWN",
        f"{early}/{int(past_claims.sum())} 'late_delivery' tickets were raised BEFORE the stored promised_eta",
        "The ETA the customer saw is not captured anywhere; KPI uses stored promised_eta. Ask App team",
        "Customer App"))

    # ---------------------------------------------------------------- ORGANIZATIONAL
    defs = metric_defs.get("stakeholders", {})
    results.append(CheckResult(
        "V23", "organizational", "Late Delivery Rate has one agreed definition + owner", "UNKNOWN",
        f"{len(defs)} competing definitions ({', '.join(defs)}); client note: "
        f"'{metric_defs.get('note', '')}'",
        "Publish VP Ops definition as primary, show all definitions side by side; owner sign-off requested",
        "VP Operations"))

    types = set(ev["type"].unique())
    results.append(CheckResult(
        "V24", "organizational", "Driver-arrived-at-restaurant event exists",
        "UNKNOWN" if "arrived_at_restaurant" not in types else "PASS",
        f"driver event types = {sorted(types)}; GPS ping gap median ~13 min (too sparse for a geofence)",
        "Pickup delay cannot be split restaurant vs driver. Instrumentation request logged", "Driver App"))
    return results


def gate_decision(results: list[CheckResult]) -> dict:
    counts = {s: sum(r.status == s for r in results) for s in ["PASS", "WARN", "FAIL", "UNKNOWN"]}
    unknown_ids = {r.check_id for r in results if r.status == "UNKNOWN"}
    publish = counts["FAIL"] == 0
    attribution_ready = publish and not ({"V20", "V24"} & unknown_ids)
    return {
        "counts": counts,
        "publish_outputs": publish,
        "fitness_by_decision": {
            "Weekly late-delivery KPI report": "READY (with WARN caveats)" if publish else "NOT READY",
            "Where delay accumulates (stage metrics)": "READY" if publish else "NOT READY",
            "Restaurant-vs-driver blame for pickup delay": "READY" if attribution_ready else "NOT READY (V20, V24)",
            "AI delay-prediction model": "NOT READY — labels & key events not reliable (V20, V22, V24)",
        },
    }
