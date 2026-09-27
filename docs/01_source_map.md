# 01 · Source map — where does the truth live? (Class 4)

**Method:** Problem → Questions → Information → Fields → Sources → Owner → Trust → Gaps. We started from the questions, not from the data folder.

## 1. Business questions → information → source

| # | Business question | Information needed | Field(s) | Source system (owner) | Retrieval | Trust |
|---|---|---|---|---|---|---|
| Q1 | How many deliveries break the promise? | promise, completion, status | `promised_eta`, `actual_delivery_at`, `final_status` | **Order Platform DB** (Commerce) | SQL | ✅ System of record (SoR) |
| Q2 | When was a driver assigned / reassigned? | assignment timeline, current driver | `assigned_at`, `reassigned_at`, `driver_id` | **Dispatch Service** (Dispatch eng.) | REST API, paginated | ✅ SoR |
| Q3 | Was pickup late vs the plan? | planned vs actual pickup | `estimated_pickup_at` / `picked_up` event | Dispatch API / **Driver App** (Driver ops) | API + nested JSON | ✅ SoR for pickup = Driver App |
| Q4 | Was the food ready / did the driver arrive late? | ready time, arrival time | `status`+`last_updated_at` / **none** | Restaurant feed (Restaurant partnerships) / — | CSV | ❌ snapshot only; **arrival not captured** |
| Q5 | Is traffic/weather to blame? | conditions per order | `traffic_bucket`, `weather_bucket`, `distance_km_estimate` | Order DB (fed by Maps/Weather) | SQL | ⚠️ context only; distance 50% defaulted |
| Q6 | How do customers react? | app behaviour, complaints | `ETA_VIEWED`, `SUPPORT_OPENED`, `CANCEL_ATTEMPTED`; ticket `category` | **Customer App analytics**, **Support platform** | CSV exports | ⚠️ perception, not events |
| Q7 | What did ops do about it, and when? | intervention type/time/actor | `intervention_type`, `intervention_at`, `initiated_by` | **Ops intervention log** (Operations) | CSV export | ⚠️ action log (intent), not outcome |
| Q8 | Which definition of "late" do we publish? | agreed KPI definition | stakeholder definitions | `client_metric_definitions.json` (no owner!) | file | ❓ organisational gap |

## 2. Source inventory (owner · grain · freshness · access)

| Source | Format | Owner | Grain (1 row =) | Freshness | Rows | Known issues found |
|---|---|---|---|---|---|---|
| `database/flasheats.db` → `orders` | SQLite | Commerce / Order Platform | order (**broken**: 3 dup rows) | batch snapshot, 1–28 Aug 2026 | 1,603 | dup rows disagree; 37 null completions; 4 promise < created; 5 pickup > delivery; pickup_at on cancelled; distance = 18.0 on 50% |
| `database/flasheats.db` → `restaurants`, `drivers` | SQLite | Restaurant partnerships / Driver ops | restaurant / driver | static | 60 / 120 | 2 impossible coordinates |
| Dispatch API `/dispatch/orders` | JSON over HTTP, 16 pages | Dispatch | order (current assignment) | live | 1,600 | transient 500 + 429; reassignment overwrites driver |
| `exports/driver_events.json` | nested JSON | Driver App | event (driver → events[]) | app sync | 10,035 events | arrays not time-sorted; no arrival event; GPS every ~13 min |
| `exports/support_tickets.csv` | CSV | Support | ticket | daily export | 202 | 1 duplicate; 3 no order_id; category spelling drift |
| `exports/customer_app_actions.csv` | CSV | Customer App analytics | app action | daily export | 2,365 | mixed timestamp precision |
| `exports/order_interventions.csv` (v2) | CSV | Operations | intervention | daily export | 260 | disagrees with Dispatch on reassignment; 12 on cancelled orders |
| `exports/restaurant_status.csv` | CSV | Restaurant partnerships (manual) | order **snapshot** (last update only) | manual, minute-rounded | 502 | 2 dup rows; 31% coverage; 104 updates after pickup |

## 3. System-of-record decisions (the reasoning written down)

| Fact | Candidates | **Chosen SoR** | Why (ownership · freshness · replication · semantics) |
|---|---|---|---|
| Customer promise | Order DB `promised_eta` · Dispatch `current_delivery_eta` | **Order DB `promised_eta`** | The promise is made once at checkout; Dispatch's ETA is revised upward (median +8 min) — measuring against it would hide 30 pts of lateness |
| Delivery completion | Order DB · Driver App `delivered` | **Order DB**, Driver App as *flagged* fallback | Order DB owns order state; the app event agrees on **100% of 1,495** overlapping orders, so it may fill the 37 blanks (`delivery_ts_source = driver_app_recovered`) |
| Pickup time | Order DB `pickup_at` · Driver App `picked_up` | **Driver App** | The app observes the event; DB value is impossible for 5 orders and populated for all 68 cancelled orders the app never picked up |
| Current driver / reassignment | Order DB `driver_id` · Dispatch · intervention log | **Dispatch API** | Dispatch creates and changes assignments; the Order DB keeps the original driver (98 mismatches, all explained); the intervention log overlaps Dispatch on only 4 of 79 reassignments |
| Food ready / restaurant state | restaurant feed | **none reliable** | Manual last-updated snapshot for 31% of orders; "preparing" after pickup — relevant ≠ authoritative |
| KPI definition | 4 stakeholder definitions | **VP Operations (proposed)** | Customer-facing definition; published with the others side by side until signed (check V23) |

> Freshness alone is not authority: a newer value from a copy is still a copy.

## 4. Minimum data — what we deliberately did NOT ingest

| Not ingested | Why |
|---|---|
| `customers` table (names, e-mails, home lat/lon) | PII not needed: `customer_id` on orders is enough. *Minimum data > all data* |
| `_not_ingested/restaurants.csv` | exact copy of the DB `restaurants` table (verified identical) — replication, not a source |
| `_not_ingested/customer_interactions.csv` | derived blend of tickets + app actions; duplicate `interaction_id`s (CI-0178…0180 reused); the two originals are used instead |
| `_not_ingested/order_outcomes.csv`, `order_events.csv` | analyst outputs from Class 7, not client systems — the pipeline rebuilds them (and reconciles: 0 mismatches, 37 "unknown" outcomes resolved) |
| `_not_ingested/order_interventions_v1_superseded.csv` | older export of the intervention log: different schema (`reason` vs `reason_code`), 430 rows vs 260. The current export matches `order_events` exactly. Feeding it in is a demo failure: `--chaos schema_drift` |

## 5. Stakeholder map (Class 1 buckets)

| Role | Who at FlashEats | What they own / need |
|---|---|---|
| Outcome owner | **VP Operations** | the late-delivery KPI; must sign its definition |
| Users of the output | Ops & Dispatch leads, Support Lead | weekly KPI + where to intervene |
| Data / system owners | Order Platform (Commerce), Dispatch eng., Driver App team, Restaurant partnerships, Support tooling | fix the data issues listed above |
| Upstream | Restaurants (manual status), drivers (app events) | the missing FOOD_READY / ARRIVED events |
| Finance | refunds / credits | wants cancelled & refunded orders excluded |
| Data team | historical dashboard | its 56.4% silently dropped 37 orders |

## 6. Gaps that change the scope

1. **No `DRIVER_ARRIVED_AT_RESTAURANT` event** and no food-ready history → pickup delay cannot be split restaurant vs driver. *Instrumentation problem, not a modelling problem.*
2. **Customer-visible ETA is not stored** → all 39 "already past the promised time" tickets were raised *before* the stored promise.
3. **No KPI owner** → the number depends on who you ask (56.5% vs 23.2%).
4. **Distance defaults to 18.0 km** for half the orders → cannot test the distance hypothesis.

→ Revised problem: *"FlashEats cannot yet attribute late deliveries because the first-mile events are not recorded — fix instrumentation before building an AI delay predictor."*
