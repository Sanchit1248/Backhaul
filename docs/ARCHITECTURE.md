# Architecture

One Python worker takes a request through ten steps. Steps 2–8 each save their output to the step log before the next starts. Every irreversible action goes through one approval queue and outbox. A kill at any moment loses no finished work and repeats no action.

```mermaid
flowchart TB
    REQ[/"Request + client records<br/>(kickoff kit)"/] --> S1

    subgraph PIPE["Pipeline: steps 2-8 checkpointed in the step log"]
        direction LR
        S1[1 Intake] --> S2[2 Discover] --> S3[3 Resolve identity] --> S4[4 Check triggers]
        S4 --> S5[5 Route fit] --> S6[6 Exclude] --> S7[7 Find buyer] --> S8[8 Draft]
    end

    S8 --> S9[9 Approval queue]
    S9 --> HUMAN((Approver<br/>web page / CLI))
    HUMAN -- approve / edit / reject --> S9
    S9 --> S10[10 Send once]
    S10 --> OUT[/"Email + .ics invite<br/>final report with evidence + cost"/]

    S2 & S3 & S4 & S5 & S7 -- "DataSources" --> FJ["fetch_json<br/>retry 429/5xx, backoff<br/>response cache"]
    FJ --> CD[(Crustdata API)]
    FJ --> WEB[(Web fetch · MCA/GST)]
    S8 -. optional polish .-> LLM[(Claude API)]

    S3 -- close matches --> HOLD1[hold: lookalike]
    S4 -- undated --> CV[cannot verify]
    S4 -- older than 30 days --> DROP[drop]
    S6 -- client / rival / DNC --> EXC[exclude]
    S7 -- buyer left, name-only --> HOLD2[hold: no verified buyer]

    subgraph STORE["SQLite ledger"]
        SL[(step log)]
        OB[("approval queue + outbox<br/>key = hash(job, prospect, action)")]
        CL[(cost ledger)]
        EV[(evidence + events)]
    end
    PIPE -.-> SL
    S9 -.-> OB
    S10 -.-> OB
    FJ -.-> CL
    LLM -.-> CL
    BUD{{"Budget check<br/>before each paid call"}} -.-> CL
    CFG[["config/service.yaml<br/>window, weights, titles,<br/>limits, template"]] -.-> PIPE
```

## Where each judged property is met
| Property | Mechanism | Code |
|---|---|---|
| Each fact has a source | `Fact` requires ≥1 `Source` with a non-empty excerpt and record date | `models.py` |
| Cannot verify, never guess | undated trigger, low-confidence identity, failed source → `CannotVerify` | `steps/s03`, `s04` |
| No lookalikes | identify top-2 gap, domain check | `steps/s03_identity.py` |
| No departed or same-name buyer | role end date, employer id must equal company id | `steps/s07_buyer.py` |
| Approval before email/invite | `ledger.request_approval` → person → `decide` / `update_payload` | `steps/s09_approve.py` |
| Each action once after a kill | outbox row with unique key; `started` → provider check → `done`; atomic file write | `steps/s10_send.py`, `mailer.py` |
| Restart resumes | step log per (job, step) | `ledger.step`, `pipeline.py` |
| Sources fail | retry with backoff, then `SourceUnavailable` → cannot verify | `sources/base.py` |
| Cost limit, cost per call | `Budget.check()` before each paid call; every call is a cost row | `budget.py`, `ledger.costs` |
| Hour-3 change, live fix | rules in one YAML file | `config/service.yaml` |

## Design question answers
- **100× volume:** several workers on a shared queue, Postgres instead of SQLite (same tables), Crustdata batch APIs, the response cache, and per-key rate limits.
- **A second client:** a `client_id` on every table, plus its own config file, budget and do-not-contact list.
