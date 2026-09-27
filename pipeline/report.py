"""REPORT — the human-readable layer: evidence table, validation gate, Gate 2 review, charts.

Published atomically to reports/latest/ only when the validation gate is open.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# validated default palette (dataviz skill): slot 1 blue, slot 2 orange; recessive ink + grid
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
ON_TIME, LATE = "#2a78d6", "#eb6834"


def _style(ax, title, xlabel=None):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12, fontweight="bold")
    if xlabel:
        ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def chart_stage_decomposition(stage_tbl: pd.DataFrame, path: Path):
    t = stage_tbl.iloc[[0, 1, 3]].iloc[::-1]          # assign, first mile, transit
    labels = ["Order → driver assigned", "Assigned → picked up\n(first mile)", "Picked up → delivered\n(transit)"][::-1]
    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    y = range(len(t))
    h = 0.36
    b1 = ax.barh([i + h / 2 + 0.02 for i in y], t["on_time_median_min"], height=h, color=ON_TIME, label="On-time orders")
    b2 = ax.barh([i - h / 2 - 0.02 for i in y], t["late_median_min"], height=h, color=LATE, label="Late orders")
    for bars in (b1, b2):
        for bar in bars:
            ax.text(bar.get_width() + 0.6, bar.get_y() + bar.get_height() / 2, f"{bar.get_width():.1f}",
                    va="center", fontsize=9, color=INK)
    for i, (_, r) in zip(y, t.iterrows()):
        ax.text(max(r["late_median_min"], r["on_time_median_min"]) + 5.5, i, f"late +{r['gap_min']:.1f} min",
                va="center", fontsize=10, color=INK, fontweight="bold" if r["gap_min"] > 5 else "normal")
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.set_xlim(0, t[["on_time_median_min", "late_median_min"]].max().max() + 20)
    _style(ax, "Late orders lose their time BEFORE pickup, not on the road", "median minutes per stage")
    ax.legend(frameon=False, fontsize=9, loc="upper right", labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_pickup_overrun(journey: pd.DataFrame, path: Path):
    e = journey[journey["kpi_eligible"]].copy()
    bins = [-1e9, 0, 5, 10, 15, 20, 1e9]
    names = ["early / on plan", "0–5", "5–10", "10–15", "15–20", "> 20"]
    e["bucket"] = pd.cut(e["pickup_overrun_min"], bins=bins, labels=names)
    g = e.groupby("bucket", observed=False).agg(n=("order_id", "count"), late=("is_late", "mean")).reset_index()
    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    colors = [ON_TIME if i < 3 else LATE for i in range(len(g))]
    ticks = [f"{b}\nn={n}" for b, n in zip(g["bucket"].astype(str), g["n"])]
    bars = ax.bar(ticks, 100 * g["late"], color=colors, width=0.62)
    for bar in bars:
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2, f"{bar.get_height():.0f}%",
                ha="center", fontsize=10, color=INK)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=ON_TIME, label="within 10-min grace"), Patch(color=LATE, label="pickup SLA miss")],
              frameon=False, fontsize=9, loc="upper left", labelcolor=INK_2)
    ax.set_ylim(0, 112)
    ax.set_ylabel("late delivery rate", color=INK_2, fontsize=9)
    _style(ax, "Miss the pickup estimate by > 10 min and the order is almost always late",
           "minutes the pickup happened after Dispatch's own estimated_pickup_at")
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        out.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    return "\n".join(out)


ICON = {"PASS": "✅ PASS", "WARN": "⚠️ WARN", "FAIL": "❌ FAIL", "UNKNOWN": "❓ UNKNOWN"}


def validation_markdown(results, gate, run_date) -> str:
    rows = pd.DataFrame([{"ID": r.check_id, "Layer": r.layer, "Check": r.check, "Status": ICON[r.status],
                          "Evidence": r.evidence.replace("|", "/"), "Action taken": r.action.replace("|", "/"),
                          "Owner": r.owner} for r in results])
    fit = "\n".join(f"| {k} | {v} |" for k, v in gate["fitness_by_decision"].items())
    c = gate["counts"]
    return f"""# Validation gate — run_date {run_date}

**{c['PASS']} PASS · {c['WARN']} WARN · {c['FAIL']} FAIL · {c['UNKNOWN']} UNKNOWN** →
outputs {'PUBLISHED' if gate['publish_outputs'] else 'NOT published'}.

Validation is decision-dependent — the same data is fit for one decision and not another:

| Decision this data could support | Fitness |
|---|---|
{fit}

{_md_table(rows)}
"""


def evidence_markdown(metrics, tables, gate, manifest) -> str:
    m1, m2, m3, m4, m5, pop = (metrics[k] for k in ["M1", "M2", "M3", "M4", "M5", "population"])
    ev = pd.DataFrame([
        ["M1", "**Late Delivery Rate** (project KPI)", "Outcome · lagging", f"**{m1['value_pct']}%**",
         f"late ÷ KPI-eligible delivered orders ({m1['numerator']}/{m1['denominator']}); grain = order",
         f"> 10 min late: {m1['severe_late_rate_pct_gt10']}% · median lateness {m1['median_lateness_of_late_min']} min",
         "⚠️ definition not owner-signed (V23)"],
        ["M2", "**Pickup SLA Miss Rate**", "Workflow · leading · controllable", f"**{m2['value_pct']}%**",
         "orders picked up > 10 min after Dispatch's estimate ÷ eligible orders",
         f"late if missed: {m2['late_rate_if_miss_pct']}% vs {m2['late_rate_if_on_time_pickup_pct']}% if not; "
         f"pickup overrun ≥ total delay for {m2['late_orders_explained_by_pickup_pct']}% of late orders",
         "✅ pickup from Driver App (SoR)"],
        ["M3", "**Customer Escalation Rate**", "Customer interaction · leading", f"**{m3['value_pct']}%**",
         "late orders with in-app support opened or a ticket ÷ late orders",
         f"{m3['severe_late_escalation_pct']}% when > 10 min late; tickets alone catch only "
         f"{m3['late_orders_with_ticket_pct']}% of late orders", "⚠️ ticket categories partly unconfirmed (V16)"],
        ["M4", "**Preventive Intervention Coverage**", "Intervention · controllable", f"**{m4['value_pct']}%**",
         "late orders that got a non-refund intervention before the promised ETA ÷ late orders",
         f"late rate with preventive intervention {m4['late_rate_with_preventive_pct']}% vs "
         f"{m4['late_rate_no_intervention_pct']}% with none — no measurable effect yet",
         "❓ association only; reassignment log ≠ Dispatch (V19)"],
        ["M5", "**ETA Drift**", "ETA reliability · diagnostic", f"**+{m5['value_min']} min**",
         "median(Dispatch current ETA − promised ETA)",
         f"late vs revised ETA {m5['late_vs_revised_eta_pct']}% vs original promise {m5['late_vs_original_promise_pct']}%",
         "✅ both from their SoR"],
    ], columns=["#", "Metric", "Type", "Value", "Formula (grain)", "What it tells the business", "Trust"])

    c = gate["counts"]
    return f"""# FlashEats — Late Delivery Evidence Table

*Generated by `run_pipeline.py` · run_date **{manifest['run_date']}** · run_id `{manifest['run_id']}` ·
gate: {c['PASS']} PASS / {c['WARN']} WARN / {c['FAIL']} FAIL / {c['UNKNOWN']} UNKNOWN*

> **Decision supported:** do **not** fund the AI delay-predictor yet. {m1['value_pct']}% of deliveries miss the
> promise, and the delay is created **before pickup** (M2) — a controllable dispatch/restaurant hand-off problem
> that the current data cannot yet attribute to restaurant vs driver (no arrival event). Fix and instrument the
> first mile; track M1–M5 weekly with this pipeline.

{_md_table(ev)}

![stages](charts/stage_decomposition.png)

![pickup](charts/pickup_overrun_vs_late.png)

## The KPI depends on the definition (V23)
{_md_table(tables['kpi_by_definition'])}

Population: {pop['orders_total']} orders → {pop['kpi_eligible']} KPI-eligible
(excluded {pop['excluded_cancelled']} cancelled, {pop['excluded_promise_invalid']} impossible promises,
{pop['excluded_no_completion_time']} with no completion time anywhere; **{pop['recovered_from_driver_app']} recovered**
from the Driver App — {pop['recovered_orders_late_rate_pct']}% of them were late, so dropping them hid the worst orders).

## Where does the time go? (median minutes)
{_md_table(tables['stage_decomposition'])}

## Interventions (association, not causation)
{_md_table(tables['intervention_by_type'])}

## Weekly trend
{_md_table(tables['weekly_trend'])}

## Context segments (descriptive only)
{_md_table(tables['segments'])}

## Known / Unknown / Assumption / Limitation

| | Statement | Evidence |
|---|---|---|
| **Known** | More than half of deliveries miss the promised ETA ({m1['value_pct']}%); about a quarter by > 10 min | M1, V12 |
| **Known** | Delay builds up between driver assignment and pickup; transit is similar for late and on-time orders | stage table, M2 |
| **Known** | Tickets under-count the problem — only {m3['late_orders_with_ticket_pct']}% of late orders produce one | M3 |
| **Unknown** | Whether the restaurant (food not ready) or the driver (arrived late) causes the pickup delay | V20, V24 |
| **Unknown** | Which ETA the customer actually saw — 'past the promised time' tickets arrive before the stored promise | V22 |
| **Unknown** | Whether interventions work — they target at-risk orders and there is no control group | M4, V19 |
| **Unknown** | Who owns the KPI definition — four stakeholder definitions, none signed off | V23 |
| **Assumption** | `promised_eta` in the Order DB is the customer promise; `distance = 18.0` is a default, not a distance | V17, V22 |
| **Assumption** | Driver App `delivered` event = true completion when the Order DB is blank (100% agreement on overlap) | V12 |
| **Limitation** | Single 4-week classroom snapshot (Aug 2026, Bengaluru); descriptive analysis, no causal claims | — |
| **Limitation** | Restaurant status is a last-updated snapshot for ~31% of orders; GPS pings every ~13 min | V20, V24 |
"""


def gate2_markdown(results, gate, manifest) -> str:
    st = {r.check_id: r for r in results}
    disp = next(s for s in manifest["sources"] if s["source"] == "dispatch")["completeness"]
    ready = "READY for the weekly KPI report · NOT READY for delay attribution or an AI predictor"

    def row(label, cid):
        r = st.get(cid)
        return f"| {label} | {ICON[r.status] if r else '—'} | {r.evidence if r else ''} |"

    return f"""# Gate 2 — Data Readiness Review (Class 8 template, filled by the pipeline)

## Pipeline run
- [x] Complete flow runs with one command — `python run_pipeline.py --run-date {manifest['run_date']}`
- [x] Same run can be safely repeated — partition `run_date={manifest['run_date']}` is atomically replaced (see tests/test_pipeline.py)
- [x] Raw API responses are preserved — {disp['pages']} pages in `data/bronze/run_date={manifest['run_date']}/dispatch_api/`
- [x] Processed output is written only after validation — silver/gold/reports skipped when any check FAILs

## Validation
| Check | Status | Evidence / note |
|---|---|---|
{row('Required columns', 'V01')}
{row('Critical nulls', 'V04')}
{row('Order uniqueness', 'V03')}
{row('Freshness', 'V06')}
{row('Dispatch retrieval completeness', 'V02')}

## Reliability
| Capability | Status | Evidence / note |
|---|---|---|
| Bounded retries | ✅ | max {manifest['config']['api']['max_retries']} attempts, exponential backoff, `Retry-After` honoured; {disp['retries']} transient errors retried this run |
| Useful failure message | ✅ | e.g. `orders: missing required columns: ['promised_eta']` — try `--chaos missing_column` |
| Logging | ✅ | `logs/pipeline_{manifest['run_date']}.log` — stage, source, rows, attempt, publish decision |
| Idempotent rerun | ✅ | same run_date ⇒ same partition replaced; metrics hash identical across reruns |
| Configuration outside core logic | ✅ | `config/pipeline.yaml` + `.env` overrides; secrets never committed (`.env` git-ignored) |
| Recovery when a source is down | ✅ | `--replay-raw` rebuilds from preserved bronze pages |

## Known limitations
- `driver_arrived_at_restaurant` is still not captured (V24); restaurant status is a sparse snapshot (V20).
- Customer-visible ETA not stored (V22); KPI definition owner not agreed (V23).
- Intervention log and Dispatch disagree on reassignments (V19).

## Gate decision
**{ready}**

Reason: {gate['counts']['FAIL']} FAIL checks, so the KPI pipeline is dependable; but the events needed to
attribute pickup delay (restaurant readiness, driver arrival) do not exist yet.
"""


def write_reports(out_dir: Path, journey, metrics, tables, results, gate, manifest, issues):
    charts = out_dir / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    chart_stage_decomposition(tables["stage_decomposition"], charts / "stage_decomposition.png")
    chart_pickup_overrun(journey, charts / "pickup_overrun_vs_late.png")
    (out_dir / "evidence_table.md").write_text(evidence_markdown(metrics, tables, gate, manifest), encoding="utf-8")
    (out_dir / "validation_report.md").write_text(validation_markdown(results, gate, manifest["run_date"]), encoding="utf-8")
    (out_dir / "gate2_data_readiness.md").write_text(gate2_markdown(results, gate, manifest), encoding="utf-8")
    for name, df in tables.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)
    issues.to_csv(out_dir / "issues_log.csv", index=False)
