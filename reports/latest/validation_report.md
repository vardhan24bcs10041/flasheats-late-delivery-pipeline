# Validation gate — run_date 2026-09-27

**11 PASS · 16 WARN · 0 FAIL · 5 UNKNOWN** →
outputs PUBLISHED.

Validation is decision-dependent — the same data is fit for one decision and not another:

| Decision this data could support | Fitness |
|---|---|
| Weekly late-delivery KPI report | READY (with WARN caveats) |
| Where delay accumulates (stage metrics) | READY |
| Restaurant-vs-driver blame for pickup delay | NOT READY (V20, V24) |
| AI delay-prediction model | NOT READY — labels & key events not reliable (V20, V22, V24) |

| ID | Layer | Check | Status | Evidence | Action taken | Owner |
|---|---|---|---|---|---|---|
| V01 | technical | Required columns present | ✅ PASS | 43 required columns across 7 sources | none |  |
| V02 | technical | Retrieval completeness (every source) | ✅ PASS | all 9 sources reconcile to their source counts; dispatch 1600/1600 over 16 pages, 2 transient errors retried | none |  |
| V03 | technical | Business grain: one row per order_id | ⚠️ WARN | 3 extra rows (0.19%) for 3 orders; duplicates disagree on: ['traffic_bucket'] | Keep first row per order_id (agreed grain); conflicting field values logged in issues_log | Order Platform |
| V04 | technical | Critical fields not null | ⚠️ WARN | critical nulls {'order_id': 0, 'created_at': 0, 'promised_eta': 0, 'final_status': 0}; soft nulls {'restaurant_id': 3, 'driver_id': 3} | Orders missing restaurant/driver kept; excluded only from restaurant/driver cuts | Order Platform |
| V05 | technical | Timestamps parseable | ✅ PASS | 24471 timestamps checked, unparseable=0; mixed precision formats in ['orders.promised_eta', 'dispatch.current_delivery_eta', 'customer_app_actions.action_at'] | Parsed with a mixed-format ISO parser (a strict parser crashes on these files) | Data platform |
| V06 | technical | Freshness SLA (weekly KPI report) | ✅ PASS | latest order created_at=2026-08-28 23:33:00; age=30 days vs allowed 45 | none | Order Platform |
| V07.1 | technical | Cross-source mapping: orders.restaurant_id -> restaurants | ⚠️ WARN | coverage=100.0% of non-null keys; null keys=3 | Unmapped/null keys kept and flagged |  |
| V07.2 | technical | Cross-source mapping: orders.driver_id -> drivers | ⚠️ WARN | coverage=100.0% of non-null keys; null keys=3 | Unmapped/null keys kept and flagged |  |
| V07.3 | technical | Cross-source mapping: orders -> dispatch record | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V07.4 | technical | Cross-source mapping: dispatch.order_id -> orders | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V07.5 | technical | Cross-source mapping: driver_events.order_id -> orders | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V07.6 | technical | Cross-source mapping: support_tickets.order_id -> orders | ⚠️ WARN | coverage=100.0% of non-null keys; null keys=3 | Unmapped/null keys kept and flagged |  |
| V07.7 | technical | Cross-source mapping: customer_app_actions.order_id -> orders | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V07.8 | technical | Cross-source mapping: order_interventions.order_id -> orders | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V07.9 | technical | Cross-source mapping: restaurant_status.order_id -> orders | ✅ PASS | coverage=100.0% of non-null keys; null keys=0 | none |  |
| V08 | lifecycle | Promise made after order creation (promised_eta >= created_at) | ⚠️ WARN | 4 orders promised before they were created (all exactly -10 min) | Flagged `promise_invalid`; excluded from the KPI denominator (lateness vs an impossible promise is meaningless). Sensitivity reported. | Order Platform / ETA service |
| V09 | lifecycle | Order DB: pickup_at before actual_delivery_at | ⚠️ WARN | 5 orders delivered before their DB pickup; Driver App pickup is chronologically valid for 5 of them | Pickup time taken from Driver App (system of record for pickup); DB value kept for audit | Order Platform |
| V10 | lifecycle | Cancelled orders carry no fulfilment evidence | ⚠️ WARN | 68/68 cancelled orders have a DB pickup_at, but Driver App shows 0 pickups — DB pickup_at looks like a scheduled/placeholder value | DB pickup_at ignored for cancelled orders; semantics question raised with Order Platform | Order Platform |
| V11 | lifecycle | Delivered orders have a completion timestamp | ⚠️ WARN | 37 delivered orders lack actual_delivery_at; 37 have a Driver App `delivered` event | Recover from Driver App only if V12 passes; flag `delivery_ts_source` | Order Platform |
| V12 | lifecycle | Driver App vs Order DB agree on delivery time (recovery allowed?) | ✅ PASS | 1495 orders present in both; 100.0% agree within 60s (max /diff/=0s) | Recovery ENABLED for missing DB timestamps |  |
| V13 | lifecycle | Current driver agrees across Order DB and Dispatch | ⚠️ WARN | 98 mismatches; 98 explained by a dispatch reassignment or a null DB driver_id | Dispatch = system of record for driver assignment; Order DB keeps the ORIGINAL driver | Dispatch |
| V14 | lifecycle | Interventions happen inside the order lifecycle | ⚠️ WARN | 260 interventions: 0 before order creation, 1 after delivery, 12 on cancelled orders | Only interventions between creation and promised ETA count as 'preventive' in metric M4 | Operations |
| V15 | lifecycle | Driver App event arrays are in time order | ⚠️ WARN | 120/120 drivers' event arrays are NOT time-ordered (gps pings listed before pickup although later in time) | Always sort by event timestamp — reading array order wrongly suggests 'no GPS after pickup' | Driver App |
| V16 | semantic | Category values map to an agreed vocabulary | ⚠️ WARN | representation fixes={'final_status': {'Delivered': 5}, 'traffic_bucket': {'HIGH': 3}, 'restaurant_status': {'READY': 1, 'Ready': 1, 'ready ': 2}, 'ticket_category': {'Late Delivery': 1, 'late_delivery ': 1}}; owner-UNconfirmed semantic mappings={'restaurant_status': {'handoff': 1}, 'ticket_category': {'ETA issue': 1}}; unmapped=none | Case/whitespace normalised via config/category_map.csv; semantic merges applied provisionally and listed for owner sign-off; unmapped values kept as UNMAPPED:<raw> | Support Lead / Restaurant Ops |
| V17 | semantic | distance_km_estimate is a measurement, not a default | ⚠️ WARN | 806 rows (50.38%) are exactly 18.0 km; 806 of them also carry a second-precision (rounded) promised_eta -> looks like a fallback ETA path | Flag `distance_is_default`; distance NOT used as a delay driver until the owner confirms | Dispatch / Maps |
| V18 | semantic | Restaurant coordinates inside Bengaluru | ⚠️ WARN | 2 restaurants outside the city box: [{'restaurant_id': 'R007', 'lat': 95.12, 'lon': 77.694095}, {'restaurant_id': 'R019', 'lat': 12.847954, 'lon': 190.33}] | Flag `coords_valid=False`; not used (no geo metric in scope) | Restaurant Partnerships |
| V19 | semantic | Reassignment: Dispatch and intervention log tell the same story | ❓ UNKNOWN | Dispatch reassigned 95 orders; intervention log records 79 DRIVER_REASSIGNMENT; overlap=4 | Dispatch = system of record for 'was it reassigned'; intervention log = what ops ASKED for. Question raised with Dispatch + Ops leads | Dispatch / Operations |
| V20 | semantic | Restaurant status feed fit to attribute pickup delay | ❓ UNKNOWN | one last-updated snapshot per order (no history); covers 31.66% of delivered orders; 104 'preparing/ready' updates arrive AFTER the driver already picked up | Not used for attribution. Needs FOOD_READY event history from restaurants | Restaurant Partnerships |
| V21 | semantic | Support tickets: unique and linkable | ⚠️ WARN | 1 exact duplicate ticket rows; 3 tickets without order_id | Exact duplicates removed; unlinked tickets kept in the ticket table but cannot join to an order | Support |
| V22 | semantic | Customer-visible ETA = stored promised_eta? | ❓ UNKNOWN | 39/39 'late_delivery' tickets were raised BEFORE the stored promised_eta | The ETA the customer saw is not captured anywhere; KPI uses stored promised_eta. Ask App team | Customer App |
| V23 | organizational | Late Delivery Rate has one agreed definition + owner | ❓ UNKNOWN | 4 competing definitions (VP Operations, Support Lead, Finance, Data Team); client note: 'No canonical KPI owner is formally documented.' | Publish VP Ops definition as primary, show all definitions side by side; owner sign-off requested | VP Operations |
| V24 | organizational | Driver-arrived-at-restaurant event exists | ❓ UNKNOWN | driver event types = ['assigned', 'delivered', 'gps_ping', 'picked_up']; GPS ping gap median ~13 min (too sparse for a geofence) | Pickup delay cannot be split restaurant vs driver. Instrumentation request logged | Driver App |
