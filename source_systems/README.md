# source_systems — the client's systems (read-only)

These files simulate FlashEats' production systems exactly as the client handed them over (Class 5–8 classroom pack, latest versions). **The pipeline never edits them**; it copies what it reads into `data/bronze/run_date=…/` on every run.

| Path | Simulates | Owner | Retrieval mode |
|---|---|---|---|
| `database/flasheats.db` | Order Platform DB (`orders`, `restaurants`, `drivers`, `customers`) | Commerce / Order Platform | **SQL** (date-window query) |
| `dispatch_api/mock_dispatch_api.py` + `dispatch_data.json` | Dispatch Service REST API — paginated, fails page 3 with HTTP 500 and page 5 with HTTP 429 once per process | Dispatch engineering | **REST API** (auto-started by the pipeline) |
| `exports/driver_events.json` | Driver App event export (driver → events[]) | Driver App team | **nested JSON** |
| `exports/support_tickets.csv` | Support platform export | Support | CSV |
| `exports/customer_app_actions.csv` | Customer App analytics export | Customer App | CSV |
| `exports/order_interventions.csv` | Ops intervention log — **current export (v2)** | Operations | CSV |
| `exports/restaurant_status.csv` | Restaurant partner status feed (manual, last update only) | Restaurant partnerships | CSV |
| `client_docs/` | client metric definitions, Class 7 brief, pack manifest | — | reference |
| `_not_ingested/` | files we **deliberately do not use** — see below | — | — |

## Why `_not_ingested/` is not ingested

| File | Reason |
|---|---|
| `restaurants.csv` | identical copy of the DB `restaurants` table — replication, not a source |
| `customer_interactions.csv` | derived blend of tickets + app actions, with re-used `interaction_id`s (CI-0178…0180) |
| `order_outcomes.csv`, `order_events.csv` | analyst outputs from Class 7, not client systems — the pipeline rebuilds them |
| `order_interventions_v1_superseded.csv` | older export of the intervention log (430 rows, column `reason` instead of `reason_code`); used only for `--chaos schema_drift` |
