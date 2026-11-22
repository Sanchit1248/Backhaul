# Pitch prep: ethics (25) and financial feasibility (25)

Half the finals points come from these two judges. Fill this in as we build.

## Financial feasibility
| Question | Answer |
|---|---|
| Who is the buyer? (role at the 3PL client) | |
| What do they pay a firm for this job today? (₹ per job / retainer, with source) | |
| Our price per job (₹) | |
| **Measured** cost per job (₹) from `cli cost` over the eval set | |
| Gross margin | |
| Jobs per month at the client → monthly revenue | |
| Why a firm would switch (speed, accuracy, audit trail) | |

Get cost per job from the ledger: run all evals, sum `costs.cost_usd` per run, convert with `fx.usd_to_inr` in config.

## Ethics
| Question | Answer |
|---|---|
| What personal data do we touch? (names, phone, PAN, bank details...) | |
| Minimisation: what we don't fetch or store | |
| Retention: where it's stored, how it's deleted | |
| Which actions need human approval, and why those | |
| How a decision is explained (sources + reasoning shown to the approver) | |
| Namesakes / lookalikes: how we avoid harming the wrong person | |
| What happens when we're unsure (cannot verify → a person) | |
| Relevant law: DPDP Act 2023 (India) | |
