# 03 · Workflow + data model (Class 7)

Raw systems are organised by **source**; the model reorganises them around the **business workflow**.

## 1. The order workflow and who observes each step

```mermaid
flowchart LR
    A([Customer places order]) -->|ORDER_CREATED<br/>order_db| B[Driver assigned]
    B -->|DRIVER_ASSIGNED / REASSIGNED<br/>dispatch_api| C{{First mile}}
    C -->|FOOD_READY ❓<br/>not recorded| D[Driver at restaurant]
    D -->|ARRIVED ❓<br/>not recorded| E[Picked up]
    C -.->|PICKED_UP<br/>driver_app| E
    E -->|transit<br/>GPS every ~13 min| F([Delivered])
    F -->|DELIVERED<br/>order_db + driver_app| G[(Outcome:<br/>late / on time)]
    P[Promise = promised_eta<br/>order_db] -.-> G
    A -.-> X[Customer reactions<br/>ETA_VIEWED · SUPPORT_OPENED · tickets]
    X -.-> I[Ops interventions<br/>reassign · contact restaurant · priority · ETA msg · refund]
    I -.-> C
    classDef gap fill:#fde2d6,stroke:#eb6834,color:#0b0b0b
    class D gap
```

**Where the time goes (median minutes, on-time → late):** assignment 2.3 → 2.3 · **first mile 16.6 → 28.8 (+12.2)** · transit 45.5 → 49.0 (+3.5). The step that loses the promise is exactly the one with no reliable events.

## 2. Entities · events · states · interactions · interventions · outcomes

| Concept | In FlashEats | Stored in |
|---|---|---|
| **Entities** (things that persist) | order, customer, restaurant, driver, support ticket | orders / customers / restaurants / drivers / tickets |
| **Events** (things that happen, time-stamped) | ORDER_CREATED, DRIVER_ASSIGNED, DRIVER_REASSIGNED, PICKED_UP, DELIVERED | `order_timeline` (gold) |
| **State** (latest event) | delivered / cancelled; restaurant preparing / ready / handed_off | `final_status`; restaurant snapshot |
| **Interactions** (customer) | ETA_VIEWED, SUPPORT_OPENED, CANCEL_ATTEMPTED, TICKET_* | app actions, tickets |
| **Interventions** (business) | DRIVER_REASSIGNMENT, RESTAURANT_CONTACT, PRIORITY_DISPATCH, ETA_MESSAGE, REFUND_OFFER | ops intervention log |
| **Outcome** | delivered_on_time / delivered_late / cancelled / not_measurable, `delay_min` | `order_journey` (gold) |

## 3. Relational model (order-centric)

```mermaid
erDiagram
    CUSTOMER ||--o{ ORDER : places
    RESTAURANT ||--o{ ORDER : prepares
    DRIVER ||--o{ ORDER : "delivers (current, per Dispatch)"
    ORDER ||--|| DISPATCH_ASSIGNMENT : "has (1:1)"
    ORDER ||--o{ ORDER_EVENT : "timeline"
    ORDER ||--o{ APP_ACTION : "customer reacts"
    ORDER ||--o{ SUPPORT_TICKET : "customer complains"
    ORDER ||--o{ INTERVENTION : "ops acts"
    ORDER ||--o| RESTAURANT_STATUS : "snapshot (31%)"
    ORDER ||--|| ORDER_JOURNEY : "aggregated to"

    ORDER {
        string order_id PK
        string customer_id FK
        string restaurant_id FK
        datetime created_at
        datetime promised_eta
        datetime picked_up_at "Driver App SoR"
        datetime delivered_at "Order DB, app fallback"
        string final_status
    }
    DISPATCH_ASSIGNMENT {
        string order_id PK
        string driver_id_current FK
        string original_driver_id
        datetime assigned_at
        datetime reassigned_at
        datetime estimated_pickup_at
        datetime current_delivery_eta
    }
    ORDER_EVENT {
        string order_id FK
        datetime event_time
        string event_type
        string actor_type
        string source_system
    }
    APP_ACTION {
        string action_id PK
        string order_id FK
        string action_type
        datetime action_at
    }
    SUPPORT_TICKET {
        string ticket_id PK
        string order_id FK "nullable (3)"
        string category
        datetime created_at
    }
    INTERVENTION {
        string intervention_id PK
        string order_id FK
        string intervention_type
        datetime intervention_at
        bool preventive
    }
    ORDER_JOURNEY {
        string order_id PK
        float delay_min
        float first_mile_min
        float pickup_overrun_min
        bool customer_escalated
        int intervention_count
        string outcome
    }
```

## 4. Tables the pipeline publishes (gold)

| Table | Grain | PK | Built from | Answers |
|---|---|---|---|---|
| `order_journey.csv` | 1 row per order (1,600) | `order_id` | orders ⋈ dispatch ⋈ (app actions, tickets, interventions **aggregated first**) | KPI, stage durations, reaction, intervention per order |
| `order_timeline.csv` | 1 row per event (9,682) | `order_id, event_time, event_type` | union of 7 source systems, time-sorted | "what happened to order X, in order?" |
| `kpi_by_definition.csv` · `stage_decomposition.csv` · `intervention_by_type.csv` · `weekly_trend.csv` · `segments.csv` | aggregate | — | `order_journey` | the evidence table |

**Rule:** aggregate one-to-many tables to the order grain *before* joining — a blunt join of orders × actions would multiply rows and inflate every rate.

**Why this model instead of mirroring the 9 source tables:** the system-of-record choices (which pickup time, which driver, which promise) are made **once**, in code, and every consumer inherits them.

## 5. Pipeline layers

```mermaid
flowchart LR
    subgraph SRC[Client systems]
      S1[(Order DB<br/>SQL)]; S2[[Dispatch API<br/>paginated REST]]; S3[Driver App<br/>nested JSON]; S4[Support · App · Ops · Restaurant<br/>CSV exports]
    end
    SRC --> EX[EXTRACT<br/>retry 429/500 · completeness proof]
    EX --> BR[(BRONZE<br/>raw, untouched<br/>per run_date)]
    BR --> VA{VALIDATE<br/>24 checks<br/>PASS/WARN/FAIL/UNKNOWN}
    VA -- any FAIL --> STOP[STOP · exit 2<br/>nothing published<br/>evidence kept in failed_runs]
    VA -- no FAIL --> CL[CLEAN → SILVER<br/>dedupe · normalise · SoR · flags · issues_log]
    CL --> TR[TRANSFORM → GOLD<br/>order_journey · order_timeline · M1–M5]
    TR --> SV[SAVE<br/>atomic swap by run_date]
    SV --> RP[reports/latest<br/>evidence table · gate · charts]
```
