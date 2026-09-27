# 02 · Validation contract (Class 6 → executable in Class 8)

**Business assumption → data expectation → executable check → severity → action.** Every row below runs on every pipeline run (`pipeline/validate.py`); live results: [`reports/latest/validation_report.md`](../reports/latest/validation_report.md).

Status meaning — **PASS** fine · **WARN** issue handled by an agreed rule, published *with* caveat · **FAIL** unsafe → stop, publish nothing · **UNKNOWN** needs a business owner, never silently resolved.

| ID | Type | Business assumption | Executable check | FAIL when… | Action taken (not silent) | Result today |
|---|---|---|---|---|---|---|
| V01 | technical | sources keep their schema | required columns per source (`config/pipeline.yaml`) | any column missing | stop; not retryable | PASS |
| V02 | technical | we got *everything* | SQL `COUNT(*)` = rows; API rows = `total_records` = unique ids; file records = rows; nested events = flattened rows | any mismatch | stop — never publish a partial extract | PASS (1,600/1,600, 2 retries) |
| V03 | technical | one row = one order | duplicate `order_id`s; do duplicates disagree? | > 1 % rows | keep first row; conflicting fields logged | WARN (3, disagree on traffic) |
| V04 | technical | KPI inputs exist | nulls in id / created / promise / status | > 1 % or any null id | soft nulls kept, excluded from per-entity cuts | WARN (3 restaurant, 3 driver) |
| V05 | technical | timestamps are usable | unparseable share; mixed precision | > 1 % | mixed-format ISO parser | PASS (24,471 parsed) |
| V06 | technical | the extract is current | age of newest order vs `--run-date` | > 45 days (weekly KPI SLA) | stop | PASS (30 days) |
| V07.1–9 | technical | sources describe the same orders | key coverage for 9 relationships | < 95 % | unmapped/null keys flagged | PASS / WARN (null keys) |
| V08 | lifecycle | promise is made after the order | `promised_eta ≥ created_at` | — | 4 orders excluded from KPI, sensitivity shown | WARN |
| V09 | lifecycle | pickup precedes delivery | DB `pickup_at ≤ actual_delivery_at` | — | pickup from Driver App (SoR) | WARN (5) |
| V10 | lifecycle | cancelled ⇒ no fulfilment | cancelled with DB pickup but no app pickup | — | DB pickup ignored; owner asked | WARN (68/68) |
| V11 | lifecycle | delivered ⇒ completion time | missing `actual_delivery_at`; recoverable? | — | recover only if V12 passes | WARN (37, all recoverable) |
| V12 | lifecycle | two sources agree before one fills the other | Driver App vs DB delivery agreement | < 99 % ⇒ recovery disabled | recovery enabled | PASS (100 % of 1,495) |
| V13 | lifecycle | "current driver" is consistent | DB vs Dispatch driver, explained by reassignment? | — | Dispatch = SoR | WARN (98, all explained) |
| V14 | lifecycle | interventions happen during the order | intervention time vs created / delivered / cancelled | — | only in-window, non-refund = "preventive" | WARN |
| V15 | lifecycle | event arrays are time-ordered | monotonic timestamps per driver | — | always sort by time | WARN (120/120 unsorted) |
| V16 | semantic | categories mean one thing | map via `config/category_map.csv` | — | case/space fixed; semantic merges listed for owner; unmapped kept as `UNMAPPED:` | WARN |
| V17 | semantic | distance is measured | share exactly 18.0 km + rounded ETA | — | flag `distance_is_default`; not used as a driver | WARN (50 %) |
| V18 | semantic | restaurants are in Bengaluru | coordinates in city box | — | flag `coords_valid` | WARN (2) |
| V19 | semantic | reassignment has one story | Dispatch vs intervention log overlap | — | Dispatch = SoR; question to Ops | UNKNOWN (4/79 overlap) |
| V20 | semantic | restaurant feed can attribute delay | coverage + updates after pickup | — | not used for attribution | UNKNOWN |
| V21 | semantic | tickets are unique & linkable | exact dups, null order_id | — | dups removed; unlinked kept | WARN |
| V22 | semantic | customer saw the stored promise | "late" tickets raised before `promised_eta` | — | ask App team | UNKNOWN (39/39) |
| V23 | organizational | KPI has one owner & definition | read client definitions | — | publish VP Ops def + all others side by side | UNKNOWN |
| V24 | organizational | driver arrival is recorded | `arrived_at_restaurant` event type exists | — | instrumentation request | UNKNOWN |

## Validation is decision-dependent

| Decision | Fit? | Blocking checks |
|---|---|---|
| Weekly late-delivery KPI report | **READY** (with WARN caveats) | — |
| Where delay accumulates (stage metrics) | **READY** | — |
| Restaurant-vs-driver blame for pickup delay | **NOT READY** | V20, V24 |
| AI delay-prediction model | **NOT READY** | V20, V22, V24 (no reliable labels/events) |

## Assumptions register (each one is testable and owned)

| Assumption | Why we make it | Who can confirm | What changes if wrong |
|---|---|---|---|
| `promised_eta` is the customer promise | only promise stored | Customer App team | KPI denominator/threshold (V22) |
| Driver App `delivered` = true completion when DB is blank | 100 % agreement on 1,495 orders | Order Platform | 37 orders return to "not measurable" (KPI 56.5 → 56.3 %) |
| Keep the first of each duplicate order row | agreed grain; 3 rows | Order Platform | traffic segment counts shift by ≤ 3 |
| `handoff` = `handed_off`, `ETA issue` = `eta_changed` | spelling drift | Restaurant Ops / Support Lead | 2 records re-categorised |
| 18.0 km is a default | 806 identical values, all with rounded ETAs | Dispatch / Maps | distance becomes a usable dimension |
