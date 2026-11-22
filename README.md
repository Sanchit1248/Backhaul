# Backhaul

**Fill every empty mile.** An outbound sales-development service for a 3PL.

**Demo Video:** [https://drive.google.com/drive/folders/1apNg_tCQgF5KQPw5oo1h5IR8Do6VglNl?usp=sharing](https://drive.google.com/file/d/1oQZeU0v1hdCjz1NPQ22CCaKSqqeprujb/view?usp=sharing)

**ContextCon 05 "Services, Replaced" · Revenue track**

A third-party logistics company (3PL) with spare trucks on a lane asks for meetings. This service
does the whole sales-development job that an SDR agency is paid for today: it finds companies
that showed a **fresh, dated logistics trigger** (hiring, expansion, funding in the last 30 days)
**near that lane**, confirms each company's identity, finds and **verifies the right buyer**, and
drafts an email and calendar invite in which **every claim links to its source**. A person approves
each email; the service then sends it **exactly once**, even if it is killed mid-send. When the
sources cannot settle a question, it says **"cannot verify"** and names the missing record instead
of guessing.

| | |
|---|---|
| **Run it** | `python -m service.web` → http://127.0.0.1:8000 (browser) · or `python -m service.cli run <request.json>` | or | https://backhaul-cyan.vercel.app
| **Live data** | Crustdata company, job, person and web APIs (default `sources.mode: live`) |
| **Measured** | Live Pune → Mumbai job, 22 Nov 2026: 100 job posts → 36 hiring companies → 9 pass filters → 4 approval-ready drafts, **₹1,179** (Crustdata-reported), **₹295 per draft** |
| **Checks** | 28 / 28 hard-case evals pass (no WRONG) · 143 tests pass, 26 skipped (saved live samples not in the repo) |
| **Design** | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · ethics and price: [docs/PITCH.md](docs/PITCH.md) |

**Contents:** [The job](#1-the-job-and-who-pays-for-it-today) · [Input](#2-input) ·
[Finished result](#3-finished-result) · [Pass mark](#4-pass-mark) ·
[Data sources](#5-data-sources-the-service-calls) · [Run it](#6-run-it) ·
[Robustness](#7-robustness-stop-restart-failures-limits) · [Logs and costs](#8-logs-and-cost-records) ·
[Changing rules](#9-changing-rules-hour-3-change-and-live-fix) · [Screening runbook](#10-screening-runbook) ·
[Tests and evals](#11-tests-and-evals) · [Personal data](#12-personal-data-and-approvals) ·
[Limitations](#13-known-limitations) · [Repo layout](#14-repo-layout)

> This is the team's prep repository. At kickoff its history moves into the organizer repository
> ([docs/TEAM.md](docs/TEAM.md#kickoff-day-runbook)); the kickoff kit's client records and request
> format then replace the test client "Fixture Logistics Pvt Ltd".

---

## 1. The job, and who pays for it today
**Job:** outbound sales development for a 3PL: find shippers that need trucks on the lanes where
the 3PL has spare capacity, qualify them, reach the right buyer, and book a first meeting.

**Who pays today:** 3PLs pay lead-generation / SDR agencies per qualified meeting or a monthly
retainer, or carry an in-house SDR team. Published Indian benchmarks (checked 22 Nov 2026, links in
[docs/PITCH.md](docs/PITCH.md)): agency pricing guides quote **$50–$200 per qualified appointment**
and **₹80,000–₹3,00,000 a month** retainers; a junior in-house SDR costs **₹4.5–7 lakh a year**.

**What we charge (our assumption):** ₹10,000 per qualified, held meeting. Measured data cost is
₹295 per draft; the client pays nothing for held or cannot-verify prospects.

## 2. Input
**A request** in plain English or JSON. The parser reads the lane, meetings wanted, spare trucks and
dates; the browser shows what it understood for confirmation before any credits are spent.

```text
Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week
3 meetings between Nashik and Mumbai, 6 spare trucks a week
Find buyers on the Pune-Mumbai lane, we have 10 free trucks
```
```json
{"job_id": "screening-1", "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week",
 "request_date": "2026-11-22", "budget_inr": 1500}
```
Optional structured fields override the text: `route`, `meetings`, `spare_trucks_per_week`,
`request_date` (the 30-day trigger window ends here), `budget_inr`.

**The client's records** (`client.records_file` in [config/service.yaml](config/service.yaml)):
lanes with spare trucks per week, current clients, rivals, and the do-not-contact list.
Example: [tests/fixtures/client_records.json](tests/fixtures/client_records.json).

## 3. Finished result
One row per company considered, each with a **status and a reason**:

| Status | Meaning |
|---|---|
| **Draft ready** (`awaiting_approval`) | Trigger, route fit and buyer all verified; email + invite waiting for a person |
| **Sent** | Approved and sent once (`.eml` + `.ics` in `data/outbox`) |
| **Held** | Lookalike identity, or no buyer could be verified, or the cost limit was reached |
| **Cannot verify** | A source could not settle it (undated news, sources disagree, source down); names the missing record |
| **Excluded** | Current client, rival, do-not-contact, or already contacted by another job in 90 days |
| **Dropped** | No trigger inside the window, or no site on the lane |

For each draft the approver sees: the email with `[n]` citations, every claim's **source (URL,
exact excerpt or API field, record date)**, the route-fit reason (a labelled rule estimate), the
buyer's evidence with four checks (employer id matches, still in the role, located in India,
business email tied to the employer), and the cost so far. Example draft (test data):

```text
To: anita.rao@kaveriagro.example
Subject: Fixture Logistics Pvt Ltd: spare truck capacity on Pune to Mumbai

Hi Anita,
I lead sales at Fixture Logistics Pvt Ltd, a third-party logistics company. I noticed Kaveri Agro
Foods posted a Logistics Manager job in Pune on 2026-11-10 [1].
... plant in Pune; spare capacity on Pune to Mumbai [2]
We have 12 trucks a week free on that lane. Would a 20-minute call next week help?

Sources: [1] https://api.crustdata.example/jobs/j-501 ...
If you would rather not hear from us, reply "unsubscribe" and we will not contact you again.
```
The full result (every company, every source, notes, cost) is saved as JSON in
`data/results/<job_id>.json` and shown on the job page.

## 4. Pass mark
| Requirement | How the service enforces it | Checked by |
|---|---|---|
| Every fact has a source | `models.Fact` cannot be built without a `Source` (URL, exact excerpt/field, record date) | model validation; evals |
| No stale trigger | trigger date must fall in `request_date − 30 days … request_date`; undated → cannot verify | `test_s04_triggers.py`; evals |
| No lookalike company | close top-2 identity matches, or id/domain mismatch → hold | evals (Shree Polymers, Tungabhadra Paper) |
| No wrong or departed buyer | employer id must equal the company id; role rechecked; located in India; non-buyer functions (IT, finance, HR, sales…) never match | `test_live_buyers.py`, `test_titles.py`; evals |
| No action without approval | every email/invite goes through the approval queue; the web app cannot send | `test_web.py` (`never_imports_sender`) |
| Each action once after a restart | outbox row per hash(job, prospect, action); file mailer checks before writing | `test_kill.py` (kill at every step and mid-send) |
| No double contact across jobs | same recipient or company queued/sent by another job in 90 days → excluded | `test_recontact.py` |
| Cost and time limits | budget check before every step and every paid call; ₹1,500 / 40 min per job | `test_budget.py`; eval `fixture_budget_stop` |

A wrong answer counts against us more than "cannot verify", so every ambiguous case holds.

## 5. Data sources the service calls
| Source | Used for | Cost (list price, 1 credit = $0.30 = ₹25.20) |
|---|---|---|
| Crustdata **job search** | discovery (logistics/warehouse/supply-chain posts in the lane's cities and satellite towns, last 30 days) and hiring triggers with posting date and location | 0.03 credits per result |
| Crustdata **company identify** | one Crustdata id per company; employer names on posts without an id | 0.2 credits per match (unmatched free) |
| Crustdata **company search** | industry and headcount filters for the hiring companies, by id | 0.03 credits per result (+ field premiums) |
| Crustdata **company enrich** | funding and news links (step 4), office locations (step 5) | 2 credits per call, up to 2 calls per company |
| Crustdata **web search + fetch** | expansion news: dated passage naming the company | 1 credit per page |
| Crustdata **person search** | buyers at the employer id, filtered to India | 0.03 credits per result |
| Crustdata **person enrich** | current role recheck + business email tied to the employer id | 1 credit (+1 with business email) |
| Client records (kickoff kit) | lanes, spare trucks, clients, rivals, do-not-contact | – |

- **Not called:** personal emails, phone numbers or personal posts (never requested). Claude is not
  used in the current pipeline: drafts come from a template filled only with sourced facts;
  `llm.ask` exists (cost-logged, budget-checked) for optional wording polish that may add no facts.
- **Planned, not implemented:** MCA/GST registry lookup to separate lookalikes, company-website
  fallbacks for locations.
- **Key limits (checked 22 Nov 2026):** 5 requests/minute on identify, enrich and search; web
  search/fetch and business-email enrichment work; batch work-history verification is not enabled.

## 6. Run it
**Setup (once):**
```powershell
.\scripts\setup.ps1          # venv, pip install -e ".[dev]", creates .env, runs the tests
```
Put `CRUSTDATA_API_KEY=` and `APPROVER_PASSWORD=` in `.env` (never committed).

### In the browser
```powershell
python -m service.web        # open http://127.0.0.1:8000 and log in (name + APPROVER_PASSWORD)
```
1. **New job:** type the request (or click an example) → **Next**.
2. **Check what was understood:** lane, meetings, spare trucks, request date, cost limit. Tick
   **Rehearsal** to use test data without spending credits. **Run the job**.
3. **Job page:** a 10-step tracker with counts per step, a cost meter against the limit and elapsed
   time (a live job takes ~5–10 minutes at 5 requests/minute). Then summary tiles (click to filter)
   and one line per company with a plain-language reason; click a company for every source.
4. **Review drafts:** email beside its evidence, `[n]` citations highlight their source, buyer
   checks with ticks. **Approve / Edit / Reject**, then **Send approved** (each email + invite once).
5. **Stop** hard-kills a running job; **Resume** continues from the last finished step.

### On the command line
```powershell
python -m service.cli run data\requests\my_job.json            # live (default)
python -m service.cli run evals\cases\fixture_hard_cases.json --mode fixtures   # rehearsal
python -m service.cli pending                                  # drafts waiting (--json)
python -m service.cli approve <key> --by "Your Name"           # or reject
python -m service.cli run data\requests\my_job.json            # sends approved ones, once
python -m service.cli outbox <job_id>                          # .eml/.ics per sent email
```
Browser jobs run this same command as a background process, so both behave identically and CLI
jobs appear in the browser's job list. Running the same `job_id` again resumes it; a new job needs
a new `job_id`.

## 7. Robustness: stop, restart, failures, limits
| Situation | What happens | Status |
|---|---|---|
| Killed at any moment (Ctrl+C, closing the terminal, Task Manager, **Stop**) | Steps 2–8 each checkpoint their output; the same command resumes at the first unfinished step (`step …: resumed from checkpoint`) | resumes |
| Killed right after an email was written | the outbox row is `started`; on restart the mailer finds the file and marks it done, never writes a second email | each action once |
| A source times out / rate-limits | retried with backoff (honours `Retry-After` and rate-limit reset headers); then "cannot verify: source unavailable" for that company only | job continues |
| A step crashes (bad response, missing key) | finished work kept, remaining companies held as cannot verify, error logged; the step is not checkpointed, so Resume retries it | `partial` |
| Cost limit reached | checked before every step and before every paid call (using the call's maximum credit estimate) | `stopped_limit` with a partial result |
| Same person reached by two jobs | excluded before queueing and re-checked right before sending | `excluded` |

Rehearse an exact crash point: `.\scripts\demo.ps1` (run → approve → crash with
`SERVICE_CRASH_AT=after_send` → restart → exactly one email and one invite → cost ledger), or
`-Mode fixtures` to rehearse without credits. Crash points: `after:02_discover` … `after:08_draft`,
`before_send`, `after_send`.

## 8. Logs and cost records
```powershell
python -m service.cli logs <job_id>           # step-by-step log (emails and phone numbers masked)
python -m service.cli cost <job_id>           # ₹ per call type + Crustdata credits estimated vs reported
python -m service.cli cost <job_id> --calls   # every paid call: status, credits, request id, time
```
Every Crustdata call writes a cost row with the estimated maximum credits, the credits Crustdata
reported (`X-Credits-Used`), the credits charged to the budget, and the request id. On 22 Nov 2026
our ledger matched Crustdata's reported credits except for calls that timed out without a reported
figure, which the ledger books at the estimate, so it can only over-state cost, never under-state it.
Repeated identical calls are answered from a local cache at no cost.

## 9. Changing rules (hour-3 change and live fix)
Every tunable lives in [config/service.yaml](config/service.yaml); a rule change is a config edit,
then `python evals\run_evals.py`.

| To change | Edit |
|---|---|
| Trigger window, trigger weights, ignored types, non-freight job titles | `triggers.*` |
| Cost / time limits, companies per live job | `limits.*`, `discovery.live_max_candidates` |
| Target industries, headcount band, discovery breadth | `discovery.*` |
| Buyer titles, excluded functions, junior titles, countries | `buyer.*` |
| Route-fit scores and satellite towns (Chakan → Pune, Bhiwandi → Mumbai …) | `route_fit.*` |
| Email subject, template, sender | `email.*` |
| Re-contact window | `approval.recontact_days` |
| **Output fields** | `output.prospect_fields`: a named field or any dotted path into a prospect (`buyer.role_start`, `triggers.0.event_date`, `route_fit.score`); `output.result_fields` for job-level fields |

A typo in an output field stops the job at start, before any step or send.

## 10. Screening runbook
Run from this machine: `data/` (ledger, logs, cost records, outbox, cache) is not in git.

1. **Check:** `python -m pytest -q` and `python evals\run_evals.py` → no WRONG.
2. **Start:** `python -m service.web`, log in, type the unseen request, confirm, **Run the job**
   (live). Or save it as JSON and `python -m service.cli run data\requests\screening.json`.
3. **Explain the result:** tiles, then each company's reason; open a draft to show sources and
   buyer checks.
4. **Approve** one draft (a person, never the service) → **Send approved** → `svc outbox <job_id>`.
5. **Stop + restart** when asked: **Stop** (or kill the process) → **Resume** (or the same command);
   the tracker shows "resumed after restart"; the outbox still holds one email per approval.
6. **Logs and costs:** `svc logs <job_id>`, `svc cost <job_id> --calls`.

| You see | Meaning / do |
|---|---|
| `partial` + "step … failed" | a source broke; the rest is held. Press Resume (or re-run): it retries that step |
| `stopped_limit` | cost limit reached; raise the job's cost limit (or `limits.max_cost_inr`) and Resume |
| "port already in use" | the app is already running: open http://127.0.0.1:8000, or `--port 8001` |
| No drafts | read each reason: "no trigger in 30 days", "no verified buyer", "already contacted" are correct holds, not errors |

(`svc` = `python -m service.cli`.)

## 11. Tests and evals
```powershell
python -m pytest -q              # 143 pass, 26 skipped (tests that replay saved live samples)
python evals\run_evals.py        # 28 hard cases; exit code 1 on any WRONG
```
**Evals** score each company's outcome like screening does (WRONG is worse than cannot verify):
lookalike names, old and undated triggers, departed and same-name buyers, current client, rival and
do-not-contact, sources that disagree, a source outage, and a budget stop mid-job.
**Tests** cover the kill/restart guarantees (a real subprocess killed at every step and mid-send),
the approval web app, cross-job re-contact, retention and log masking, buyer titles, live data
mapping (company, signals, buyers) and the browser job flow end to end.

## 12. Personal data and approvals
- Only work data about people: name, title, employer, role dates, business email
  (`privacy.person_fields`). Business email is kept only if Crustdata ties it to the verified
  employer id. No personal emails, phones or posts are requested.
- A person is never matched on name alone: the employer id must equal the company's id.
- Every email and invite needs a named approver; edits may not add facts (every `[n]` must map to a
  sourced claim) and must keep the unsubscribe line.
- Logs mask emails and phone numbers; person data is deleted after 30 days (`privacy.retention_days`)
  while keeping proof that an email was sent once.

Full ethics answers: [docs/PITCH.md](docs/PITCH.md#ethics).

## 13. Known limitations
- **Kickoff kit pending:** drafts use the test client "Fixture Logistics Pvt Ltd" until the real
  client records and request format are mapped into `steps/s01_intake.py`.
- **Coverage:** about 60% of job posts carry no Crustdata company id; employer-name lookup recovers
  some, but multi-entity groups (e.g. LANXESS, ABB, Lupin) are left out as lookalikes rather than
  guessed. The company index filters on headquarters, so hiring posts are the main location signal.
- **Speed:** the key allows 5 requests/minute, so a live job takes ~5–10 minutes; one job runs at a time.
- **Sending:** the mailer writes `.eml` + `.ics` files (`email.mailer: file`); SMTP to a test inbox
  is not implemented.
- **Scale:** single machine with SQLite; see the design answers in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#15-design-questions).
- **Request parsing** is pattern-based; the confirm screen catches anything it misreads.

## 14. Repo layout
```text
config/service.yaml          every rule, limit, price, template and output field
src/service/
  models.py                  shared data contract: Fact, Source, CannotVerify, Prospect, Trigger, Buyer
  pipeline.py                runs the ten steps; 2-8 checkpointed, 9-10 idempotent; partial / stopped_limit
  steps/s01..s10_*.py        intake, discover, identity, triggers, route fit, exclude, buyer, draft, approve, send
  sources/                   DataSources interface; live.py + live_*.py (Crustdata); crustdata.py client;
                             base.py (fetch_json: cache, retries, budget, cost rows); fixtures.py (tests)
  ledger.py                  SQLite: runs, step checkpoints, approval queue + outbox, cost ledger, events
  budget.py  faults.py       cost/time limits; crash points for kill drills
  privacy.py                 log masking; retention settings
  mailer.py                  .eml + Outlook/Google-safe .ics, idempotent file send
  web/app.py  web/jobs.py    browser app; background jobs (same `svc run` process)
  cli.py                     svc run / pending / approve / reject / outbox / cost / logs / purge
evals/                       hard-case eval cases + scorer
tests/                       unit, kill/restart, web and live-mapping tests; fixtures
scripts/                     setup.ps1, demo.ps1 (finals demo), sync.py
docs/                        architecture, pitch (ethics + finance), plan, checklist, problem statement
```
