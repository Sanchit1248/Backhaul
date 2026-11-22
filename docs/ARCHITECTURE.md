# Architecture

Required deliverable: show the components, the data flow, and **where the data calls, approvals, retries and the cost ledger sit**. Update this diagram once the track is chosen. GitHub renders Mermaid natively.

```mermaid
flowchart LR
    REQ[/Client request<br/>kickoff-kit format/] --> CLI[CLI / API entry<br/>run_id = hash of request]
    CLI --> P

    subgraph P[Pipeline: each step checkpointed]
        direction LR
        I[intake] --> G[gather] --> D[decide] --> A[act] --> R[report]
    end

    G -- "fetch_json<br/>retry + backoff + cache" --> SRC[(Live sources<br/>Crustdata · registries · web)]
    D -- "extract / match / explain" --> LLM[(Claude API)]
    D -->|no source settles it| CV[cannot verify<br/>+ what is missing]

    A --> GATE{irreversible?}
    GATE -- no --> FX[perform once<br/>idempotency key]
    GATE -- yes --> Q[[Approval queue]] --> HUMAN((Person)) --> FX
    FX --> PAY[(Razorpay / Stripe<br/>TEST mode, email, calendar)]

    subgraph L[SQLite ledger: survives kill -9]
        ST[steps<br/>checkpoints]
        AC[actions<br/>at-most-once]
        CO[costs<br/>per call]
        EV[events<br/>log]
    end
    P -.-> ST
    FX -.-> AC
    SRC -.-> CO
    LLM -.-> CO
    BUD[Budget guard<br/>cost / time / calls] -.checks.-> CO
    R --> OUT[/Result: every fact linked to<br/>source URL + exact field + record date/]
```

## How each judged property is met

| Property | Mechanism | Code |
|---|---|---|
| Each claim has a source | `Fact` won't construct without a `Source` that has an excerpt | `evidence.py` |
| "Cannot verify" instead of guessing | `decide` emits `CannotVerify(missing=...)` when no record settles it | `pipeline.py` |
| Approval before irreversible actions | `approval.irreversible` in config → `pending_approval` until `cli approve` | `ledger.action` |
| Kill on stage, each action once | step checkpoints + idempotency key passed to the provider as well | `ledger.py` |
| Sources fail | retry with backoff, then `SourceUnavailable` → cannot verify | `sources/base.py` |
| Time and cost limits | `Budget.check()` before every paid call; every call logged | `budget.py`, `costs` table |
| Requirement change at hour 3 | limits, required fields and prices live in `config/service.toml` | config |

## Scaling answers (system design question)
- **100× volume:** move from SQLite to Postgres (same schema), run the pipeline per item on a queue (e.g. Redis/RQ or Celery), cache source lookups per entity, batch LLM calls through the Batch API at half price.
- **Second client:** add `client_id` to every table and config profile; keep per-client records and approval rules separate.
