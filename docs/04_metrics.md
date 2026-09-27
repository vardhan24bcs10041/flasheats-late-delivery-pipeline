# 04 · Metrics — linked to the project KPI (Class 3 + 7)

**Project KPI:** reduce **Late Delivery Rate** (client brief: `class7_model_brief.json`).
**SMART framing (proposed to VP Ops):** *reduce Late Delivery Rate from 56.5 % toward ≤ 40 % within 6 weeks by cutting pickup-SLA misses from 42 % to ≤ 25 %, without changing the driver fleet size.* Targets are proposals for the outcome owner, not facts.

```text
PROJECT KPI     M1 Late Delivery Rate  56.5 %  (lagging · outcome)
  ├─ driver     M2 Pickup SLA Miss Rate 42.0 %  (leading · controllable)   late if missed 96.9 % vs 27.2 %
  ├─ reaction   M3 Customer Escalation  29.9 %  (leading CX signal)        52.3 % when > 10 min late
  ├─ lever      M4 Preventive Intervention Coverage 15.2 % (controllable)  no measurable effect yet
  └─ honesty    M5 ETA Drift  +8.1 min  (diagnostic)                       late vs revised ETA 26.2 %
```

| | M1 Late Delivery Rate | M2 Pickup SLA Miss Rate | M3 Customer Escalation Rate | M4 Preventive Intervention Coverage | M5 ETA Drift |
|---|---|---|---|---|---|
| **Formula** | orders with `delivered_at > promised_eta` ÷ KPI-eligible orders | orders with `picked_up_at > estimated_pickup_at + 10 min` ÷ eligible | late orders with `SUPPORT_OPENED` or a ticket ÷ late orders | late orders with a non-refund intervention between creation and promise ÷ late orders | median(`current_delivery_eta − promised_eta`) |
| **Grain** | order | order | order | order | order |
| **Population** | delivered, completion from Order DB or validated Driver App, promise ≥ creation (1,528) | same | late orders (863) | late orders | eligible orders |
| **Sources** | Order DB, Driver App | Dispatch API, Driver App | Customer App, Support | Ops log, Order DB | Dispatch API, Order DB |
| **Type** | outcome · lagging | workflow · leading | customer interaction | intervention | diagnostic |
| **Why it matters** | the promise the customer sees broken | the first mile is where late orders lose 12 min | CX damage and support load | are we acting *before* the promise breaks? | explains "the ETA keeps changing" |
| **Owner** | VP Operations | Dispatch lead + Restaurant Ops | Support Lead | Ops lead | Dispatch / ETA team |
| **Today** | **56.5 %** (> 10 min: 23.2 %) | **42.0 %** | **29.9 %** | **15.2 %** | **+8.1 min** |
| **Caveat** | definition not signed (V23) | can't split restaurant vs driver (V24) | categories partly unconfirmed (V16) | association only (V19, selection bias) | — |

## Leading vs lagging (Class 3)
- **Lagging:** M1 — confirms the business result weekly.
- **Leading:** M2 (known the moment pickup happens, *before* delivery) and M3 (support opens ~11 min after the promise breaks) — tell you within the day whether a first-mile fix is working.

## Sensitivity — the KPI depends on the definition
| Definition | Orders | Late % |
|---|---|---|
| **VP Operations — any delay** (published) | 1,528 | **56.5** |
| Support Lead — > 10 min | 1,528 | 23.2 |
| Finance — exclude cancelled/refunded | 1,528 | 56.5 |
| Data Team historical dashboard (DB only, no recovery, no promise check) | 1,495 | 56.4 |

Leadership's "56 %" is reproducible only under one definition; it roughly halves under the Support Lead's.

## Metrics we rejected
| Candidate | Why not |
|---|---|
| Tickets per 100 orders as the KPI | only 19 % of late orders produce a ticket — measures complaints, not lateness |
| Late rate by distance band | 50 % of distances are a default value (V17) |
| Restaurant "prep time" | no food-ready history (V20) |
| Late rate vs Dispatch's current ETA | moving target — would report 26 % instead of 56 % |
