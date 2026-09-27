# 05 · Decision log — the FDE judgement calls

Each call: **context → decision → evidence → consequence → how it is kept honest.**

### D1 ⭐ Recover the 37 "missing" deliveries instead of dropping them (demo call)
- **Context:** 37 delivered orders have no `actual_delivery_at`. The Class 5 reference answer and the client's historical dashboard exclude them.
- **Decision:** fill them from the Driver App `delivered` event, but only after proving the two sources are equivalent.
- **Evidence:** on the 1,495 orders present in both, the timestamps agree **100 % (0 s difference)** — check V12. The recovered orders are **64.9 % late** vs 56.4 % overall → excluding them hid the *worst* orders (survivorship bias).
- **Consequence:** denominator 1,495 → 1,532; late rate 56.4 % → 56.6 % (then 56.5 % after D4).
- **Kept honest:** `delivery_ts_source = driver_app_recovered` on every recovered row; the recovery switches **off automatically** if agreement drops below 99 %; the issues log lists all 37; Data Team's number is still shown side by side.

### D2 Pickup time comes from the Driver App, not the Order DB
- 5 orders have a DB pickup *after* delivery (the app's pickup is valid); all 68 cancelled orders have a DB `pickup_at` the app never recorded. The app observes the event; the DB field is derived. → V09, V10.

### D3 Dispatch is the system of record for the driver
- The Order DB keeps the *original* driver (98 mismatches, all explained by reassignment or nulls). The intervention log's 79 reassignments overlap Dispatch's 95 on only 4 → the log records what Ops *asked for*, Dispatch records what *happened*. → V13, V19 (UNKNOWN, question raised).

### D4 Exclude 4 impossible promises from the KPI
- 4 orders were "promised" exactly 10 min before they were created, and they top the "worst delayed" list with ~70–87 min of lateness — lateness against an impossible promise is meaningless. Excluded, flagged `promise_invalid`, counted in the report. → V08.

### D5 Quarantine distance as an explanatory variable
- 806 orders (50 %) are exactly 18.0 km, all with second-rounded promised ETAs → a fallback code path, not a measurement. Distance is flagged `distance_is_default` and not used to explain delay until Dispatch/Maps confirm. → V17.

### D6 Publish the VP Ops definition — with every other definition next to it
- No KPI owner exists; 56.5 % (any delay) vs 23.2 % (> 10 min) are different business problems. We publish one headline and the full sensitivity table rather than silently picking. → V23.

### D7 Use the current intervention export (v2), keep v1 as evidence
- Two exports of the same system exist (430 rows / `reason` vs 260 rows / `reason_code`). v2 matches `order_events` (same 260 orders); v1 does not. Pointing the pipeline at v1 is a controlled failure (`--chaos schema_drift` → V01 FAIL).

### D8 Minimum data: no customer PII, no derived files as sources
- `customers` (names, e-mails, home coordinates) is not needed for this KPI. `customer_interactions.csv`, `order_outcomes.csv`, `order_events.csv` and `restaurants.csv` are copies/derivations — ingesting them would double-count. → docs/01_source_map.md §4.

### D9 Do not build the AI delay predictor yet (the "revised problem")
- The delay is created in the first mile (+12.2 min for late orders; 96.9 % of pickup-SLA misses end late), but the events that would attribute it (FOOD_READY, DRIVER_ARRIVED) do not exist, and the customer-visible ETA is not stored. A model would learn to predict a label the system cannot yet define or act on. **Instrument first; track M1–M5 weekly with this pipeline; then evaluate prediction.**
