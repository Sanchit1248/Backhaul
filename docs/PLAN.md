# Plan: outbound SDR service for a 3PL (Revenue track)

Copy of the team's plan doc. The source of truth is the team's Claude Doc; update this when it changes.

## Summary
The bot books a first meeting between the 3PL's sales head and the right buyer at a company that has shown a real expansion trigger in the last 30 days and ships near routes where the 3PL has spare truck capacity. It does the whole sales development job, from the request to the sent invite. A person approves each email and invite before it goes out.

| Item | Definition |
|---|---|
| Job | Outbound sales development for a 3PL: find, qualify and book meetings with new shippers |
| Who pays for it today | Lead-generation and SDR agencies (per meeting or monthly retainer), or the salaries of an in-house SDR team |
| Input | A request, e.g. "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week", plus client records: routes, capacity, current clients, do-not-contact list |
| Finished result | Per prospect: trigger with source and date, route-fit reason, buyer with source, approved email, sent invite. Or a hold reason, or "cannot verify" with what is missing |
| Pass mark | Each fact sourced. No trigger older than 30 days. No lookalike company or departed buyer contacted. No action without approval. No duplicate email/invite after restart. Each job under the cost limit |

## Scope
**Core (must ship by hour 5):** request mode; three dated, sourced trigger types (logistics/warehouse hiring, new plant/warehouse news, funding); identity check first; route-fit score with reason; buyer search + still-there check; email from sourced facts with claim→evidence links; approval page (approve/edit/reject); .ics invite with the approved email; step log, idempotency keys, cost ledger, rules in one YAML.

**Stretch:** background mode (Crustdata watcher → webhook → same pipeline); Google Calendar API; Slack approval buttons; churn flags for existing clients.

**Cut:** ML forecasts, supplier graph, own crawler, personal emails/phones/posts, auto-send without approval, reply handling, voice, dashboards.

**Later (design question only):** rival weakness signals, backhaul pricing, multiple 3PL clients, CRM sync.

## The ten steps
1. Intake → job record (job id, route filter, meetings, budget, request date)
2. Discover candidates (industry, headcount band, near route end points, hiring/expansion in 30 days)
3. Resolve identity (one Crustdata id; close top-2 → hold lookalike)
4. Check triggers (source passage + date within 30 days, else drop)
5. Route fit (plant/warehouse/office cities vs routes with spare capacity; HQ only → partial)
6. Exclude current clients, rivals, do-not-contact
7. Find buyer + confirm current employer and role start (left → next person or hold)
8. Draft from sourced facts only
9. Approve (person sees email, evidence, cost)
10. Send email + invite once (idempotency key); final report with evidence and costs

## Hard cases and decision rules
| Case | Rule | Output |
|---|---|---|
| Lookalike company | ≥2 close identify matches, or domain mismatch | Hold: "lookalike" + names |
| Trigger older than 30 days | date < request date − 30 | Reject trigger; drop if none left |
| Trigger with no date | no date in source | Cannot verify the trigger |
| Same event in two sources | same company + type, dates within 3 days | Count once, keep both sources |
| Sources disagree | different facts | Cannot verify; show both |
| Buyer has left | employer ≠ company, or role end date | Next person; none → hold "no verified buyer" |
| Same-name person | name matches, profile/company id doesn't | Not a match |
| Client / rival / DNC | on client-record list | Exclude with reason |
| Layoffs/downsizing | not a prospect trigger | Ignore (churn flag stretch) |
| Budget almost used | next call would exceed limit | Stop discovery, finish in-progress, partial result |
| Source fails | error after retries | "cannot verify: source unavailable"; continue |

## Data sources
| Need | Main | Fallback |
|---|---|---|
| Identity | Crustdata company identify | MCA/GST legal name |
| Hiring trigger | Crustdata job search | careers page via web fetch |
| Expansion news | Crustdata web search → fetch passage | company press page |
| Funding | Crustdata enrichment | news search |
| Locations | Crustdata enrichment | GST principal place; website locations |
| Buyer + still there | Crustdata person search/enrichment, work history | none: hold |
| Client routes etc. | kickoff records | none |
| Calendar | .ics | Google Calendar (stretch) |

Not available from any source: who supplies whom, prospect shipping volumes and routes. Never stated as facts.

## Ethics, pricing, demo
See docs/PITCH.md and the team doc. Price: per meeting held (main); monthly per watched route (stretch). Benchmark: 11x from $3,750/month annual.

**Finals demo (3 min):** unseen request → prospects with triggers + sources; show one hold and one cannot-verify; approve one email; judge kills during send; restart → outbox shows one email, one invite; show cost ledger.

**Likely live fixes:** window days, new output field, new buyer title, lower cost limit: all config edits.

## Open questions
- Which Revenue request will organizers send: discovery flow, or inbound leads answered within 2 minutes?
- Do client records include spare capacity per route, or only carriers?
- Real emails during screening, or test inbox only?
- Which Crustdata endpoints do event credits cover?
