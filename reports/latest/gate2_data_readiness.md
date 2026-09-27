# Gate 2 — Data Readiness Review (Class 8 template, filled by the pipeline)

## Pipeline run
- [x] Complete flow runs with one command — `python run_pipeline.py --run-date 2026-09-27`
- [x] Same run can be safely repeated — partition `run_date=2026-09-27` is atomically replaced (see tests/test_pipeline.py)
- [x] Raw API responses are preserved — 16 pages in `data/bronze/run_date=2026-09-27/dispatch_api/`
- [x] Processed output is written only after validation — silver/gold/reports skipped when any check FAILs

## Validation
| Check | Status | Evidence / note |
|---|---|---|
| Required columns | ✅ PASS | 43 required columns across 7 sources |
| Critical nulls | ⚠️ WARN | critical nulls {'order_id': 0, 'created_at': 0, 'promised_eta': 0, 'final_status': 0}; soft nulls {'restaurant_id': 3, 'driver_id': 3} |
| Order uniqueness | ⚠️ WARN | 3 extra rows (0.19%) for 3 orders; duplicates disagree on: ['traffic_bucket'] |
| Freshness | ✅ PASS | latest order created_at=2026-08-28 23:33:00; age=30 days vs allowed 45 |
| Dispatch retrieval completeness | ✅ PASS | all 9 sources reconcile to their source counts; dispatch 1600/1600 over 16 pages, 2 transient errors retried |

## Reliability
| Capability | Status | Evidence / note |
|---|---|---|
| Bounded retries | ✅ | max 4 attempts, exponential backoff, `Retry-After` honoured; 2 transient errors retried this run |
| Useful failure message | ✅ | e.g. `orders: missing required columns: ['promised_eta']` — try `--chaos missing_column` |
| Logging | ✅ | `logs/pipeline_2026-09-27.log` — stage, source, rows, attempt, publish decision |
| Idempotent rerun | ✅ | same run_date ⇒ same partition replaced; metrics hash identical across reruns |
| Configuration outside core logic | ✅ | `config/pipeline.yaml` + `.env` overrides; secrets never committed (`.env` git-ignored) |
| Recovery when a source is down | ✅ | `--replay-raw` rebuilds from preserved bronze pages |

## Known limitations
- `driver_arrived_at_restaurant` is still not captured (V24); restaurant status is a sparse snapshot (V20).
- Customer-visible ETA not stored (V22); KPI definition owner not agreed (V23).
- Intervention log and Dispatch disagree on reassignments (V19).

## Gate decision
**READY for the weekly KPI report · NOT READY for delay attribution or an AI predictor**

Reason: 0 FAIL checks, so the KPI pipeline is dependable; but the events needed to
attribute pickup delay (restaurant readiness, driver arrival) do not exist yet.
