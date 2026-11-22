# Requirements checklist (from the problem statement)

Full text: [ContextCon05_BITS_Problem_Statement.pdf](ContextCon05_BITS_Problem_Statement.pdf)

## Every service must
- [ ] Do the **whole job**, from request to finished result (not a tool for a human)
- [ ] Use **live real-world data** for at least one decision (mock data only in tests)
- [ ] Link **every fact** to its record: URL + exact passage/field + record date. No facts from model memory
- [ ] Output **"cannot verify"** + what's missing when sources can't settle it
- [ ] Hold **irreversible actions** (payment, client email) for a person's approval; payments in **test mode**
- [ ] Enforce a **time limit and cost limit** per run; **record the cost of each call**
- [ ] Pass hidden test cases dated **after 1 Sep 2026**, incl. duplicates, lookalike names, unanswerable questions
- [ ] Survive **stop + restart** with each action happening once
- [ ] Meet the **hour-3 requirement change**

## Deliverables
- [ ] Code committed throughout to the **organizer repo**
- [ ] README: job + who pays today, input, finished result, pass mark, every data source
- [ ] Architecture diagram: components, data flow, data calls, approvals, retries, cost ledger
- [ ] Logs and cost records available at screening

## Finals scoring (100)
| Area | Pts | Our prep |
|---|---|---|
| Live run (CS) | 20 | unseen request → correct sourced result |
| Robustness (CS) | 15 | kill on stage, each action once |
| Design (CS) | 15 | 5-min live fix + system design question |
| Ethics | 25 | personal data handling, approvals, explanations → [PITCH.md](PITCH.md) |
| Financial feasibility | 25 | buyer, ₹ price, measured cost per job → [PITCH.md](PITCH.md) |

A wrong answer counts against us more than "cannot verify". **When in doubt, don't guess.**
