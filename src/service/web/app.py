"""Browser app: type a request, confirm what was understood, watch the job run step by
step, review every company and draft with its sources, approve/edit/reject, and send.

Rules this file keeps:
  * decisions only call ledger.get_action / pending_approvals / decide / update_payload;
    it never imports the mailer or step 10. Jobs (including "send approved") run as the
    same `svc run` process as the CLI (web/jobs.py), so step 10 is the only sender;
  * every decision records the approver's name (from login);
  * an edit may change subject + body only (not the recipient or the invite), must keep
    the unsubscribe line, and may only cite [n] markers that map to a sourced claim.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import date, datetime
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..config import env, settings, usd_to_inr
from ..ledger import Ledger
from ..steps.s01_intake import load_client_records, parse_request
from .jobs import JobRunner, new_job_id

COOKIE = "svc_approver"
MAX_BODY = 10_000
_MARKER = re.compile(r"\[(\d+)\]")

EXAMPLES = [
    "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week",
    "3 meetings between Nashik and Mumbai, 6 spare trucks a week",
    "Find buyers on the Pune-Mumbai lane, we have 10 free trucks",
]

STEPS = [("01_intake", "Read request"), ("02_discover", "Find hiring companies"),
         ("03_identity", "Confirm identity"), ("04_triggers", "Check fresh triggers"),
         ("05_route_fit", "Match to your lane"), ("06_exclude", "Exclusions"),
         ("07_buyer", "Find & verify buyer"), ("08_draft", "Draft emails"),
         ("09_approve", "Your approval"), ("10_send", "Send")]


def create_app(ledger: Ledger | None = None, password: str | None = None,
               session_secret: str | None = None, runner: JobRunner | None = None) -> FastAPI:
    app = FastAPI(title="SDR service", docs_url=None, redoc_url=None, openapi_url=None)
    led = ledger or Ledger()
    jobs = runner or JobRunner(led.path.parent)
    pw = password if password is not None else (env("APPROVER_PASSWORD") or "")
    secret = (session_secret or env("APPROVER_SESSION_SECRET") or secrets.token_hex(32)).encode()
    lock = threading.Lock()  # one sqlite connection shared by the threadpool

    # ---- session ---------------------------------------------------------
    def sign(value: str) -> str:
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    def make_cookie(name: str) -> str:
        n = base64.urlsafe_b64encode(name.encode()).decode()
        return f"{n}.{sign('session:' + n)}"

    def approver(request: Request) -> str | None:
        raw = request.cookies.get(COOKIE, "")
        n, _, mac = raw.partition(".")
        if not n or not hmac.compare_digest(mac, sign("session:" + n)):
            return None
        try:
            return base64.urlsafe_b64decode(n.encode()).decode()
        except (ValueError, UnicodeDecodeError):
            return None

    def csrf_for(request: Request) -> str:
        return sign("csrf:" + request.cookies.get(COOKIE, ""))

    def require(request: Request, csrf: str | None = None) -> str:
        name = approver(request)
        if name is None:
            raise HTTPException(401, "log in first")
        if csrf is not None and not hmac.compare_digest(csrf, csrf_for(request)):
            raise HTTPException(403, "bad form token; reload the page")
        return name

    def pending_action(key: str) -> dict:
        a = led.get_action(key)
        if a is None:
            raise HTTPException(404, "no such action")
        if a["status"] != "pending_approval":
            raise HTTPException(409, f"action is {a['status']}, not pending approval")
        return a

    def back(msg: str, next_: str = "/") -> RedirectResponse:
        dest = next_ if re.fullmatch(r"/(jobs(/[\w.-]+)?|approvals)?", next_ or "") else "/"
        return RedirectResponse(f"{dest}?msg={quote(msg)}", status_code=303)

    def job_row(job_id: str) -> dict:
        with lock:
            run = led.get_run(job_id)
            cost = usd_to_inr(led.run_cost(job_id))
        req = jobs.request(job_id)
        meta = jobs.meta(job_id)
        return {"id": job_id, "status": jobs.status(job_id, run), "cost": cost,
                "text": req.get("text", ""), "req": req,
                "mode": meta.get("mode") or settings()["sources"]["mode"],
                "result": jobs.result(job_id), "created": meta.get("created"), "meta": meta}

    def shell(request: Request, title: str, body: str, tab: str = "", head: str = "") -> str:
        name = approver(request) or ""
        with lock:
            n_pending = len(led.pending_approvals())
        return layout(title, body, name=name, tab=tab, pending=n_pending,
                      client=client_name(), head=head)

    # ---- login -----------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "login_enabled": bool(pw)}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(error: str = "") -> str:
        note = ("" if pw else "<p class=err>APPROVER_PASSWORD is not set in .env: "
                "login is disabled.</p>")
        err = f"<p class=err>{escape(error)}</p>" if error else ""
        return layout("Welcome back", f"""<div class=narrow>{note}{err}
<form method=post action=/login class=card>
  <p class=muted>Sign in to run jobs and approve emails. Your name is recorded on every
    decision you make.</p>
  <label>Your name
    <input name=name required maxlength=80 autocomplete=name></label>
  <label>Password<input name=password type=password required></label>
  <button class="btn primary lg">Log in</button>
</form></div>""", client=client_name())

    @app.post("/login")
    def login(name: str = Form(...), password: str = Form(...)) -> Response:
        name = " ".join(name.split())[:80]
        if not pw:
            raise HTTPException(503, "APPROVER_PASSWORD is not set")
        if not name or not hmac.compare_digest(password.encode(), pw.encode()):
            return RedirectResponse("/login?error=" + quote("wrong name or password"), 303)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, make_cookie(name), httponly=True, samesite="strict",
                     max_age=12 * 3600)
        return r

    @app.post("/logout")
    def logout() -> Response:
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    # ---- tab 1: new job --------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str = "") -> Response:
        if approver(request) is None:
            return RedirectResponse("/login", status_code=303)
        token = csrf_for(request)
        busy = jobs.running_job()
        busy_html = (f"<div class='flash info'>A job is running: <a href='/jobs/{escape(busy)}'>"
                     f"watch it</a>. A new job can start when it finishes.</div>" if busy else "")
        chips = "".join(f"<button type=button class=chip data-example=\"{escape(e)}\">"
                        f"{escape(e)}</button>" for e in EXAMPLES)
        recent = [job_row(j) for j in jobs.job_ids()[:4]]
        recent_html = "".join(render_job_card(j) for j in recent)
        return HTMLResponse(shell(request, "New job", f"""{flash(msg)}{busy_html}
<section class="card hero">
  <h2>What should the service do?</h2>
  <p class=muted>Describe the lane and how many meetings you want. The service finds companies
  with a fresh logistics trigger (hiring, expansion, funding in the last
  {settings()['triggers']['window_days']} days) near that lane, verifies the right buyer, and
  drafts a sourced email for your approval.</p>
  <form method=post action=/jobs/parse>
    <input type=hidden name=csrf value="{token}">
    <textarea name=prompt id=prompt rows=3 required maxlength=1000
      placeholder="{escape(EXAMPLES[0])}"></textarea>
    <div id=preview class=preview aria-live=polite></div>
    <div class=chips><span class=muted>Try:</span>{chips}</div>
    <div class=row><button class="btn primary lg">{ICONS['spark']}<span>Check what I understood</span>
      {ICONS['arrow']}</button><span class=muted><kbd>Ctrl</kbd> + <kbd>Enter</kbd></span></div>
  </form>
</section>
{"<h2>Recent jobs</h2><div class=jobgrid>" + recent_html + "</div>" if recent else ""}""",
                                  tab="new"))

    @app.get("/jobs/preview")
    def preview(request: Request, q: str = "") -> dict:
        """What the parser understands so far (read-only; shown while typing)."""
        require(request)
        j = parse_request({"text": " ".join(q.split())[:1000],
                           "request_date": date.today().isoformat()})
        return {"route": [c.title() for c in j.route_filter], "meetings": j.meetings_wanted,
                "trucks": j.spare_trucks_per_week}

    # ---- tab 2: jobs -----------------------------------------------------
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_list(request: Request, msg: str = "") -> Response:
        if approver(request) is None:
            return RedirectResponse("/login", status_code=303)
        rows = "".join(render_job_row(job_row(j)) for j in jobs.job_ids()[:50])
        body = (f"<div class=card><table class=jobs><thead><tr><th>Started</th><th>Request</th>"
                f"<th>Status</th><th>Drafts</th><th>Cost</th></tr></thead><tbody>{rows}"
                f"</tbody></table></div>" if rows else
                "<p class=muted>No jobs yet. <a href='/'>Start one</a>.</p>")
        return HTMLResponse(shell(request, "Jobs", flash(msg) + body, tab="jobs"))

    # ---- tab 3: approvals ------------------------------------------------
    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request, msg: str = "") -> Response:
        if approver(request) is None:
            return RedirectResponse("/login", status_code=303)
        with lock:
            pending = led.pending_approvals()
            run_cost = {r: usd_to_inr(led.run_cost(r)) for r in {a["run_id"] for a in pending}}
        token = csrf_for(request)
        cards = "".join(render_action(a, run_cost[a["run_id"]], token, "/approvals", i,
                                      len(pending), [x["key"] for x in pending])
                        for i, a in enumerate(pending))
        empty = "<p class=muted>Nothing waiting for approval.</p>" if not pending else ""
        return HTMLResponse(shell(request, f"Approvals ({len(pending)})", f"""{flash(msg)}
<p class=muted>Approving marks an email approved; nothing is sent yet. Press <b>Send approved</b>
on the job page to send it (the job's send step sends each email + invite exactly once).</p>
{empty}{cards}""", tab="approvals"))

    # ---- jobs: prompt -> confirm -> run -> result -------------------------
    @app.post("/jobs/parse", response_class=HTMLResponse)
    def parse(request: Request, csrf: str = Form(...), prompt: str = Form(...)) -> Response:
        require(request, csrf)
        prompt = " ".join(prompt.split())[:1000]
        job = parse_request({"text": prompt, "request_date": date.today().isoformat()})
        route = job.route_filter + ["", ""]
        warn = ("" if len(job.route_filter) >= 2 else
                "<div class='flash bad'>I could not find two cities for the lane in your "
                "request. Fill them in below.</div>")
        default_budget = settings()["limits"]["max_cost_inr"]
        live = settings()["sources"]["mode"] == "live"
        return HTMLResponse(shell(request, "Check the request", f"""{warn}
<form method=post action=/jobs class="card narrow">
  <input type=hidden name=csrf value="{csrf_for(request)}">
  <input type=hidden name=prompt value="{escape(prompt)}">
  <p class=quote>&ldquo;{escape(prompt)}&rdquo;</p>
  <div class=grid>
    <label>Lane from<input name=from_city value="{escape(route[0].title())}" required></label>
    <label>Lane to<input name=to_city value="{escape(route[1].title())}" required></label>
    <label>Meetings wanted<input name=meetings type=number min=1 max=50
      value="{job.meetings_wanted}" required></label>
    <label>Spare trucks per week<input name=trucks type=number min=0 max=1000
      value="{job.spare_trucks_per_week if job.spare_trucks_per_week is not None else ''}"
      placeholder="from client records"></label>
    <label>Request date <span class=muted>(trigger window ends here)</span>
      <input name=request_date type=date value="{job.request_date.isoformat()}" required></label>
    <label>Cost limit (Rs)<input name=budget_inr type=number min=1 step=1
      value="{int(default_budget)}" required></label>
  </div>
  <label class=check><input type=checkbox name=rehearsal value=1 {'' if live else 'checked'}>
    <span><b>Rehearsal</b>: use test data (no Crustdata credits, finishes in seconds)</span></label>
  <p class=muted>A live job takes about 5-10 minutes (the data provider allows 5 requests a
    minute). You can leave the page; progress is saved.</p>
  <div class=row><button class="btn primary lg">{ICONS['play']}<span>Run the job</span></button>
    <a class="btn ghost" href="/">Cancel</a></div>
</form>""", tab="new"))

    @app.post("/jobs")
    def start_job(request: Request, csrf: str = Form(...), prompt: str = Form(""),
                  from_city: str = Form(...), to_city: str = Form(...),
                  meetings: int = Form(...), trucks: str = Form(""),
                  request_date: str = Form(...), budget_inr: float = Form(...),
                  rehearsal: str = Form("")) -> Response:
        name = require(request, csrf)
        route = [" ".join(from_city.split()).lower(), " ".join(to_city.split()).lower()]
        if not all(route) or route[0] == route[1]:
            return back("Give two different cities for the lane.")
        try:
            day = date.fromisoformat(request_date)
        except ValueError:
            return back("Request date must be YYYY-MM-DD.")
        job_id = new_job_id(route)
        raw = {"job_id": job_id,
               "text": prompt or f"Book {meetings} meetings for the {route[0].title()} to "
                                 f"{route[1].title()} route",
               "route": route, "meetings": int(meetings),
               "spare_trucks_per_week": int(trucks) if trucks.strip() else None,
               "request_date": day.isoformat(), "budget_inr": float(budget_inr)}
        try:
            jobs.start(job_id, raw, mode="fixtures" if rehearsal else None, by=name)
        except RuntimeError as e:
            return back(str(e))
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(job_id: str, request: Request, msg: str = "") -> Response:
        if approver(request) is None:
            return RedirectResponse("/login", status_code=303)
        if not re.fullmatch(r"[\w.-]+", job_id) or not jobs.request_path(job_id).exists():
            raise HTTPException(404, "no such job")
        j = job_row(job_id)
        with lock:
            events = led.db.execute(
                "SELECT at, level, msg FROM events WHERE run_id=? ORDER BY id", (job_id,)
            ).fetchall()
            acts = led.actions_for_run(job_id)
            run_cost = usd_to_inr(led.run_cost(job_id))
        token = csrf_for(request)
        running = j["status"] == "running"
        res_path = jobs.result_path(job_id)
        just_done = res_path.exists() and time.time() - res_path.stat().st_mtime < 8
        poll = " data-poll=1" if running or just_done else ""
        pending = [a for a in acts if a["status"] == "pending_approval"]
        approved = [a for a in acts if a["status"] in ("approved", "started", "failed")]
        sent = [a for a in acts if a["status"] == "done"]

        buttons = []
        if running:
            buttons.append(form_button(f"/jobs/{job_id}/stop", token, "Stop (hard kill)",
                                       "danger", "Kill this job now? Resume continues it from "
                                       "the last finished step.", icon="stop"))
        else:
            if j["status"] in ("interrupted", "partial", "stopped_limit", "failed"):
                buttons.append(form_button(f"/jobs/{job_id}/resume", token,
                                           "Resume from where it stopped", "primary",
                                           icon="play"))
            if approved:
                buttons.append(form_button(f"/jobs/{job_id}/send", token,
                                           f"Send approved ({len(approved)})", "primary",
                                           f"Send {len(approved)} approved email(s) with "
                                           "calendar invites? Each is sent once.", icon="send"))
        budget = float(j["req"].get("budget_inr") or settings()["limits"]["max_cost_inr"])
        res = None if running else j["result"]
        tracker = render_tracker(events, j["status"], res, len(sent))
        result_html = render_result(res) if res else (
            SKELETON if running else
            f"<pre>{escape(jobs.log_tail(job_id))}</pre>" if j["status"] == "failed" else "")
        keys = [a["key"] for a in pending]
        cards = "".join(render_action(a, run_cost, token, f"/jobs/{job_id}", i, len(pending),
                                      keys) for i, a in enumerate(pending))
        sent_html = "".join(f"<li><b>{escape(a['payload'].get('company', ''))}</b> &mdash; "
                            f"email + invite sent (approved by "
                            f"{escape(a.get('approved_by') or '?')})</li>" for a in sent)
        log = "".join(f"<li><span class=muted>{datetime.fromtimestamp(at):%H:%M:%S}</span> "
                      f"<span class='lvl {escape(lv)}'>{escape(lv)}</span> {escape(m)}</li>"
                      for at, lv, m in events[-60:])
        return HTMLResponse(shell(request, "Job", f"""{flash(msg)}<div id=live{poll}>
<section class="card jobhead">
  <div class=jobtitle>
    <div><p class=quote>&ldquo;{escape(j['text'])}&rdquo;</p>
      <p class=muted>{escape(job_id)} &middot; {'test data (rehearsal)' if j['mode'] == 'fixtures'
                                                  else 'live data'}</p></div>
    {badge(j['status'])}
  </div>
  <div class=stats>
    {render_meter(run_cost, budget)}
    <div class=stat><span class=muted>Elapsed</span><b>{elapsed(j, running)}</b></div>
    <div class=stat><span class=muted>Approved, not sent</span><b>{len(approved)}</b></div>
    <div class=stat><span class=muted>Sent</span><b>{len(sent)}</b></div>
  </div>
  <div class=row>{''.join(buttons)}</div>
</section>
{tracker}
{result_html}
{"<h2 id=drafts>Waiting for your approval (" + str(len(pending)) + ")</h2>" + cards if pending else ""}
{"<h2>Sent</h2><ul class=sent>" + sent_html + "</ul>" if sent else ""}
<details class="card logbox" {'open' if running else ''}><summary>Progress log</summary>
<ul class=log>{log}</ul></details></div>""", tab="jobs"))

    def _rerun(job_id: str, request: Request, csrf: str, reason: str) -> Response:
        name = require(request, csrf)
        if not jobs.request_path(job_id).exists():
            raise HTTPException(404, "no such job")
        try:
            jobs.start(job_id, by=name, reason=reason)
        except RuntimeError as e:
            return back(str(e), f"/jobs/{job_id}")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/resume")
    def resume(job_id: str, request: Request, csrf: str = Form(...)) -> Response:
        return _rerun(job_id, request, csrf, "resume")

    @app.post("/jobs/{job_id}/send")
    def send(job_id: str, request: Request, csrf: str = Form(...)) -> Response:
        return _rerun(job_id, request, csrf, "send approved")

    @app.post("/jobs/{job_id}/stop")
    def stop(job_id: str, request: Request, csrf: str = Form(...)) -> Response:
        require(request, csrf)
        killed = jobs.stop(job_id)
        return back("Job killed. Press Resume to continue from the last finished step."
                    if killed else "Job was not running.", f"/jobs/{job_id}")

    # ---- decisions -------------------------------------------------------
    @app.post("/actions/{key}/approve")
    def approve(key: str, request: Request, csrf: str = Form(...),
                next: str = Form("/approvals")) -> Response:  # noqa: A002
        name = require(request, csrf)
        with lock:
            a = pending_action(key)
            led.decide(key, True, by=name)
        return back(f"Approved: {a['payload'].get('company', key)} (by {name}). "
                    "Press 'Send approved' on the job page to send it.", next)

    @app.post("/actions/{key}/reject")
    def reject(key: str, request: Request, csrf: str = Form(...),
               next: str = Form("/approvals")) -> Response:  # noqa: A002
        name = require(request, csrf)
        with lock:
            a = pending_action(key)
            led.decide(key, False, by=name)
        return back(f"Rejected: {a['payload'].get('company', key)} (by {name}).", next)

    @app.get("/actions/{key}/edit", response_class=HTMLResponse)
    def edit_form(key: str, request: Request, error: str = "") -> Response:
        if approver(request) is None:
            return RedirectResponse("/login", status_code=303)
        with lock:
            a = pending_action(key)
        d = a["payload"]["draft"]
        err = f"<div class='flash bad'>{escape(error)}</div>" if error else ""
        claims = "".join(render_claim(i, c, key) for i, c in enumerate(d.get("claims", []), 1))
        return HTMLResponse(shell(request, f"Edit: {a['payload'].get('company', '')}", f"""{err}
<div class=review>
<form method=post action="/actions/{escape(key)}/edit" class=card>
  <input type=hidden name=csrf value="{csrf_for(request)}">
  <p><b>To:</b> {escape(d.get('to', ''))} <span class=muted>(recipient and invite
    cannot be edited)</span></p>
  <label>Subject<input name=subject value="{escape(d.get('subject', ''))}"
    required maxlength=200></label>
  <label>Body<textarea name=body rows=18 required class=mono>{escape(d.get('body', ''))}</textarea>
  </label>
  <p class=muted>Keep the unsubscribe line. Do not add facts: every [n] must point at one of
    the sourced claims.</p>
  <div class=row><button class="btn primary">{ICONS['check']}<span>Save edit (stays pending)</span>
    </button><a class="btn ghost" href="/approvals">Cancel</a></div>
</form>
<section class=card><h3>Sourced claims you may cite</h3><ul class=claims>{claims}</ul></section>
</div>""", tab="approvals"))

    @app.post("/actions/{key}/edit")
    def edit(key: str, request: Request, csrf: str = Form(...), subject: str = Form(...),
             body: str = Form(...)) -> Response:
        name = require(request, csrf)
        body = body.replace("\r\n", "\n")
        with lock:
            a = pending_action(key)
            payload = a["payload"]
            problem = check_edit(subject, body, len(payload["draft"].get("claims", [])))
            if problem:
                return RedirectResponse(
                    f"/actions/{quote(key)}/edit?error={quote(problem)}", status_code=303)
            payload["draft"] = {**payload["draft"], "subject": subject.strip(), "body": body,
                                "edited_by": name}
            led.update_payload(key, payload, by=name)
        return back(f"Saved edit to {payload.get('company', key)} (by {name}); "
                    "still pending approval.", "/approvals")

    return app


def check_edit(subject: str, body: str, n_claims: int) -> str | None:
    """Return why an edit is not allowed, or None."""
    if not subject.strip():
        return "subject is empty"
    if "\n" in subject or "\r" in subject:
        return "subject must be one line"
    if not body.strip():
        return "body is empty"
    if len(body) > MAX_BODY:
        return f"body is longer than {MAX_BODY} characters"
    if "unsubscribe" not in body.lower():
        return "the unsubscribe line must stay in the body"
    bad = sorted({int(m) for m in _MARKER.findall(body) if not 1 <= int(m) <= n_claims})
    if bad:
        return (f"citation(s) {', '.join(f'[{b}]' for b in bad)} do not match a sourced "
                f"claim (there are {n_claims})")
    return None


# ---- helpers -------------------------------------------------------------------

_CLIENT: dict = {}


def client_name() -> str:
    path = settings()["client"]["records_file"]
    if _CLIENT.get("path") != path:
        try:
            _CLIENT.update(path=path, name=load_client_records(path).name)
        except Exception:  # noqa: BLE001 - header only
            _CLIENT.update(path=path, name="")
    return _CLIENT["name"]


def flash(msg: str) -> str:
    return (f"<div class=toast role=status><span>{escape(msg)}</span>"
            f"<button type=button class=toast-x aria-label=Dismiss>&times;</button></div>"
            if msg else "")


SKELETON = """<section class="card skeleton" aria-hidden=true>
  <div class="sk sk-title"></div><div class=sk-tiles><div class=sk></div><div class=sk></div>
  <div class=sk></div><div class=sk></div></div>
  <div class="sk sk-row"></div><div class="sk sk-row"></div><div class="sk sk-row"></div>
  <p class=muted>Working&hellip; results appear here as soon as the job finishes.</p></section>"""


STATUS_LABEL = {"awaiting_approval": "Draft ready", "sent": "Sent", "rejected": "Rejected",
                "held": "Held", "cannot_verify": "Cannot verify", "excluded": "Excluded",
                "dropped": "Dropped", "active": "In progress", "running": "Running",
                "done": "Done", "partial": "Partial", "stopped_limit": "Cost limit hit",
                "interrupted": "Interrupted", "failed": "Failed", "not started": "Not started"}
STATUS_CLASS = {"awaiting_approval": "good", "sent": "good", "done": "good",
                "running": "info", "held": "warn", "cannot_verify": "warn", "partial": "warn",
                "stopped_limit": "warn", "interrupted": "warn", "excluded": "muted",
                "dropped": "muted", "rejected": "muted", "failed": "bad"}


def badge(status: str) -> str:
    return (f"<span class='badge {STATUS_CLASS.get(status, 'muted')}'>"
            f"{escape(STATUS_LABEL.get(status, status.replace('_', ' ')))}</span>")


def plain_reason(status: str, reason: str | None) -> str:
    """The step's reason in words a client understands (the raw reason stays in the detail)."""
    r = reason or ""
    window = settings()["triggers"]["window_days"]
    if status == "awaiting_approval":
        return "Draft ready: waiting for your approval"
    if status == "sent":
        return "Email and calendar invite sent"
    if status == "rejected":
        return "Rejected by the approver"
    if r.startswith("lookalike"):
        return ("More than one company matches this name, so it is held instead of guessing ("
                + r.split(":", 1)[-1].strip() + ")")
    if r.startswith("no trigger within"):
        older = re.search(r"\((\d+) older", r)
        return (f"No logistics hiring, expansion or funding news in the last {window} days"
                + (f" ({older.group(1)} older item(s) ignored)" if older else ""))
    if "only undated" in r:
        return "Found news without a date, so it cannot be shown to be recent"
    if r.startswith("sources disagree"):
        return "Sources disagree about the event, so it is held"
    if r.startswith("no route fit"):
        known = re.search(r"\(known: ([^)]*)\)", r)
        return ("No known site on your lane"
                + (f" (known sites: {known.group(1)})" if known else ""))
    if r == "no verified buyer":
        return ("No buyer could be verified (right role, still in the job, based in India, "
                "business email)")
    if r.startswith("already contacted"):
        return "Already contacted by an earlier job, so not emailed twice"
    if "do-not-contact" in r:
        return "On the client's do-not-contact list"
    if "current client" in r:
        return "Already the client's customer"
    if "rival" in r:
        return "A rival of the client"
    if r.startswith("cannot verify: step"):
        return "A data source failed at this step; held (Resume re-tries it)"
    if r.startswith("budget limit"):
        return "Cost limit reached before this company was finished"
    if status == "cannot_verify":
        return "Cannot verify: " + (r or "a source could not settle it")
    return r or STATUS_LABEL.get(status, status)


def outcome_step(p: dict) -> str | None:
    """Which step decided this company (for the counts on the tracker)."""
    st, r = p.get("status"), p.get("reason") or ""
    if st in ("awaiting_approval", "sent", "rejected"):
        return "08_draft"
    if r.startswith("lookalike") or any("Which company" in (c.get("question") or "")
                                        for c in p.get("cannot_verify") or []) \
            and st == "cannot_verify" and not p.get("triggers"):
        return "03_identity"
    if r.startswith(("no trigger", "sources disagree")) or "undated" in r:
        return "04_triggers"
    if r.startswith("no route fit"):
        return "05_route_fit"
    if st == "excluded":
        return "06_exclude"
    if r == "no verified buyer":
        return "07_buyer"
    return None


def elapsed(j: dict, running: bool) -> str:
    runs = (j.get("meta") or {}).get("runs") or []
    if not runs:
        return "&ndash;"
    start = runs[-1]["at"]
    end = time.time()
    res = j.get("result")
    if not running and res and res.get("generated_at"):
        try:
            end = datetime.fromisoformat(res["generated_at"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    secs = max(0, int(end - start))
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def render_meter(cost: float, budget: float) -> str:
    pct = min(100, cost / budget * 100) if budget else 0
    cls = "bad" if pct >= 90 else "warn" if pct >= 70 else "good"
    return (f"<div class='stat meter'><span class=muted>Cost</span>"
            f"<b>Rs {cost:,.0f} <span class=muted>of Rs {budget:,.0f}</span></b>"
            f"<div class=bar><div class='fill {cls}' style='width:{pct:.1f}%'></div></div></div>")


def render_tracker(events: list, status: str, res: dict | None, n_sent: int) -> str:
    msgs = [m for _, _, m in events]
    done = {m.split()[1].rstrip(":") for m in msgs if m.startswith("step ") and m.endswith(": done")}
    resumed = {m.split()[1].rstrip(":") for m in msgs if "resumed from checkpoint" in m}
    failed = next((m.split()[0] for m in reversed(msgs)
                   if re.match(r"\d\d_\w+ failed:", m) or re.match(r"\d\d_\w+: cost limit", m)),
                  None)
    found = next((re.search(r"discover: (\d+) candidates", m).group(1) for m in reversed(msgs)
                  if re.search(r"discover: (\d+) candidates", m)), None)
    counts: dict[str, int] = {}
    for p in (res or {}).get("prospects", []):
        s = outcome_step(p)
        if s:
            counts[s] = counts.get(s, 0) + 1
    started = bool(msgs)
    final = status in ("done", "awaiting_approval", "partial", "stopped_limit")
    current_set = False
    items = []
    for key, label in STEPS:
        if key == "01_intake":
            state = "done" if started else "todo"
        elif key == "09_approve":
            state = ("done" if final and not (res or {}).get("prospects") is None
                     and any(p.get("status") in ("awaiting_approval", "sent", "rejected")
                             for p in (res or {}).get("prospects", [])) else
                     "todo")
            if state == "done" and status == "awaiting_approval":
                state = "wait"
        elif key == "10_send":
            state = "done" if n_sent else "todo"
        elif key in done or key in resumed:
            state = "done"
        elif failed and failed.startswith(key):
            state = "failed"
        elif status == "running" and not current_set:
            state, current_set = "current", True
        else:
            state = "todo"
        note = ""
        if key == "02_discover" and found:
            note = f"{found} companies"
        elif key == "08_draft" and counts.get(key):
            note = f"{counts[key]} drafted"
        elif counts.get(key):
            note = f"{counts[key]} stopped here"
        if key in resumed:
            note = (note + " &middot; " if note else "") + "resumed after restart"
        if key == "09_approve" and state == "wait":
            note = "waiting for you"
        items.append(f"<li class='{state}'><span class=dot></span><span class=lbl>"
                     f"{escape(label)}</span><span class=note>{note}</span></li>")
    return f"<ol class=tracker>{''.join(items)}</ol>"


_SVG = ('<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>')
ICONS = {k: _SVG.format(v) for k, v in {
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "play": '<path d="m6 4 14 8-14 8Z"/>',
    "arrow": '<path d="M5 12h14M13 5l7 7-7 7"/>',
    "down": '<path d="M12 5v14M5 13l7 7 7-7"/>',
    "spark": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6Z"/>',
}.items()}


def form_button(action: str, csrf: str, label: str, cls: str = "", confirm: str = "",
                icon: str = "") -> str:
    dc = f' data-confirm="{escape(confirm)}"' if confirm else ""
    ic = ICONS.get(icon, "")
    return (f'<form method=post action="{escape(action)}" class=inline{dc}>'
            f'<input type=hidden name=csrf value="{csrf}">'
            f'<button class="btn {cls}">{ic}<span>{escape(label)}</span></button></form>')


def _drafts(res: dict | None) -> int:
    return sum(1 for p in (res or {}).get("prospects", [])
               if p.get("status") in ("awaiting_approval", "sent"))


def render_job_row(j: dict) -> str:
    res = j["result"] or {}
    when = (datetime.fromtimestamp(j["created"]).strftime("%d %b %H:%M")
            if j.get("created") else "&ndash;")
    mode = " <span class=tag>test data</span>" if j["mode"] == "fixtures" else ""
    return (f"<tr onclick=\"location='/jobs/{escape(j['id'])}'\" class=link-row>"
            f"<td><a href='/jobs/{escape(j['id'])}'>{when}</a>{mode}</td>"
            f"<td>{escape(j['text'][:110])}</td><td>{badge(j['status'])}</td>"
            f"<td>{_drafts(res) if res else ''}</td><td>Rs {j['cost']:,.0f}</td></tr>")


def render_job_card(j: dict) -> str:
    res = j["result"] or {}
    mode = "<span class=tag>test data</span>" if j["mode"] == "fixtures" else ""
    return (f"<a class='card jobcard' href='/jobs/{escape(j['id'])}'>"
            f"<div class=row>{badge(j['status'])}{mode}</div>"
            f"<p>{escape(j['text'][:120])}</p>"
            f"<p class=muted>{_drafts(res) if res else 0} drafts &middot; Rs {j['cost']:,.0f}</p></a>")


def _link(url: str) -> str:
    u = escape(url)
    if url.startswith(("http://", "https://")):
        short = escape(re.sub(r"^https?://(www\.)?", "", url)[:60])
        return f'<a href="{u}" target=_blank rel="noopener noreferrer" title="{u}">{short}</a>'
    return f"<code>{u}</code>"


def render_source(s: dict) -> str:
    bits = [f"<span class=tag>{escape(s.get('kind', '').replace('_', ' '))}</span>",
            _link(s.get("url", ""))]
    date_ = s.get("record_date")
    bits.append(f"record date <b>{escape(date_)}</b>" if date_
                else "<span class=err>no record date</span>")
    field = (f"<div class=muted><code>{escape(s['field_path'])}</code></div>"
             if s.get("field_path") else "")
    return (f"<li>{' &middot; '.join(bits)}{field}<blockquote>{escape(s.get('excerpt', ''))}"
            "</blockquote></li>")


def render_claim(i: int | None, fact: dict, key: str = "") -> str:
    srcs = "".join(render_source(s) for s in fact.get("sources", []))
    anchor = f" id='c-{escape(key)}-{i}'" if i is not None and key else ""
    label = f"<span class=num>[{i}]</span> " if i is not None else ""
    return (f"<li{anchor} class=claim>{label}{escape(fact.get('claim', ''))}"
            f"<ul class=src>{srcs}</ul></li>")


STATUS_ORDER = ["awaiting_approval", "sent", "rejected", "held", "cannot_verify", "excluded",
                "dropped", "active"]


def render_result(res: dict) -> str:
    ps = sorted(res.get("prospects", []),
                key=lambda p: STATUS_ORDER.index(p.get("status"))
                if p.get("status") in STATUS_ORDER else 99)
    counts: dict[str, int] = {}
    for p in ps:
        counts[p.get("status", "?")] = counts.get(p.get("status", "?"), 0) + 1
    tiles = "".join(
        f"<button type=button class='tile {STATUS_CLASS.get(k, 'muted')}' data-filter='{escape(k)}'>"
        f"<b data-count={v}>{v}</b><span>{escape(STATUS_LABEL.get(k, k))}</span></button>"
        for k, v in sorted(counts.items(), key=lambda kv: STATUS_ORDER.index(kv[0])
                           if kv[0] in STATUS_ORDER else 99))
    rows = []
    for p in ps:
        c = p.get("company") or {}
        st = p.get("status", "")
        t = (p.get("triggers") or [None])[0]
        detail = []
        if t:
            detail.append(f"<h4>Trigger</h4><ul class=claims>{render_claim(None, t.get('fact') or {})}</ul>")
        fit = p.get("route_fit")
        if fit:
            facts = "".join(render_claim(None, f) for f in fit.get("facts") or [])
            detail.append(f"<h4>Route fit <span class=muted>(rule estimate)</span></h4>"
                          f"<p>{escape(fit.get('reason') or '')}</p><ul class=claims>{facts}</ul>")
        b = p.get("buyer")
        if b:
            detail.append(f"<h4>Buyer</h4><p><b>{escape(b['name'])}</b>, {escape(b['title'])}</p>"
                          f"<ul class=claims>{''.join(render_claim(None, f) for f in b.get('facts') or [])}</ul>")
        rej = p.get("buyers_rejected") or []
        if rej:
            detail.append("<h4>Buyers considered and rejected</h4><ul>" + "".join(
                f"<li>{escape(x.get('name', ''))} &mdash; {escape(x.get('reason') or x.get('outcome') or '')}</li>"
                for x in rej) + "</ul>")
        cv = p.get("cannot_verify") or []
        if cv:
            detail.append("<h4>Cannot verify</h4><ul>" + "".join(
                f"<li>{escape(x.get('question', ''))} <span class=muted>Missing: "
                f"{escape(x.get('missing', ''))}</span></li>" for x in cv) + "</ul>")
        if p.get("reason"):
            detail.append(f"<p class=muted>Recorded reason: <code>{escape(p['reason'])}</code></p>")
        trig_short = escape(t.get("summary", "")) if t else ""
        rows.append(f"""<details class=prospect data-status="{escape(st)}">
<summary><span class=co><b>{escape(c.get('name', ''))}</b>
  <span class=muted>{escape(c.get('industry') or '')}</span></span>
  {badge(st)}<span class=why>{escape(plain_reason(st, p.get('reason')))}
  {('<br><span class=muted>' + trig_short + '</span>') if trig_short else ''}</span></summary>
<div class=pdetail>{''.join(detail) or '<p class=muted>No further detail.</p>'}</div>
</details>""")
    notes = "".join(f"<li>{escape(n)}</li>" for n in res.get("notes", []))
    return f"""<section class=card id=result>
<div class=row><h2>Result</h2>{badge(res.get('status', ''))}
  <span class=muted>Rs {res.get('cost_inr', 0):,.2f}</span></div>
<div class=tiles>{tiles}<button type=button class='tile all' data-filter=''>
  <b>{len(ps)}</b><span>All</span></button></div>
<div class=plist>{''.join(rows) or '<p class=muted>No companies found.</p>'}</div>
{"<details><summary class=muted>Notes from this run</summary><ul class=muted>" + notes + "</ul></details>" if notes else ""}
</section>"""


def buyer_checks(b: dict) -> str:
    facts = b.get("facts") or []
    excerpts = " ".join(s.get("excerpt", "") for f in facts for s in f.get("sources") or [])
    checks = []
    if b.get("employer_id") and b.get("employer_id") == b.get("company_id"):
        checks.append(("ok", "employer id matches company"))
    else:
        checks.append(("bad", "employer id does not match the company id: check before approving"))
    if b.get("role_end"):
        checks.append(("bad", f"left the role on {b['role_end']}"))
    elif any("rechecked" in str(f.get("value", "")) for f in facts):
        checks.append(("ok", "still in the role (rechecked)"))
    else:
        checks.append(("warn", "current role not rechecked"))
    m = re.search(r"location\.country = '([^']+)'", excerpts)
    checks.append(("ok", f"based in {m.group(1)}") if m else ("warn", "location not stated"))
    if any(str(f.get("claim", "")).startswith("Business email") for f in facts):
        checks.append(("ok", "business email tied to the employer id"))
    elif b.get("business_email"):
        checks.append(("warn", "business email (source not shown)"))
    else:
        checks.append(("bad", "no business email"))
    icon = {"ok": "&#10003;", "warn": "!", "bad": "&#10007;"}
    return "<ul class=checks>" + "".join(
        f"<li class={c}><span>{icon[c]}</span>{escape(t)}</li>" for c, t in checks) + "</ul>"


def render_action(a: dict, run_cost_inr: float, csrf: str, next_: str = "/approvals",
                  idx: int = 0, total: int = 1, keys: list[str] | None = None) -> str:
    p, key = a["payload"], a["key"]
    d = p.get("draft", {})
    k = escape(key)
    claims = "".join(render_claim(i, c, key) for i, c in enumerate(d.get("claims", []), 1))
    body = _MARKER.sub(lambda m: f"<a class=cite href='#c-{k}-{m.group(1)}' "
                                 f"data-c='c-{k}-{m.group(1)}'>[{m.group(1)}]</a>",
                       escape(d.get("body", "")))

    fit = p.get("route_fit_detail")
    if fit:
        fit_html = (f"<p><b>{escape(fit['level'])}</b> fit, score {fit['score']:.2f} "
                    f"<span class=muted>(rule estimate)</span>: {escape(fit['reason'])}</p>"
                    f"<ul class=claims>{''.join(render_claim(None, f) for f in fit.get('facts', []))}</ul>")
    else:
        fit_html = f"<p>{escape(str(p.get('route_fit', '')))}</p>"

    b = p.get("buyer")
    if b:
        role = escape(b.get("role_start") or "unknown")
        buyer_html = (f"<p><b>{escape(b['name'])}</b>, {escape(b['title'])} at "
                      f"{escape(b.get('employer_name') or '?')} &middot; in role since {role}</p>"
                      f"{buyer_checks(b)}<details><summary class=muted>Buyer sources</summary>"
                      f"<ul class=claims>{''.join(render_claim(None, f) for f in b.get('facts', []))}"
                      f"</ul></details>")
    else:
        buyer_html = ("<p class=muted>Buyer evidence not in this payload (queued by an older "
                      "version of step 9).</p>")

    edited = (f" <span class=tag>edited by {escape(d['edited_by'])}</span>"
              if d.get("edited_by") else "")
    queued = p.get("cost_inr_so_far")
    queued_s = f"Rs {queued:.2f} when queued &middot; " if isinstance(queued, int | float) else ""
    nxt = ""
    if keys and idx + 1 < len(keys):
        nxt = (f"<a class='btn ghost' href='#a-{escape(keys[idx + 1])}'>"
               f"<span>Next draft</span>{ICONS['down']}</a>")
    hidden = (f'<input type=hidden name=csrf value="{csrf}">'
              f'<input type=hidden name=next value="{escape(next_)}">')
    return f"""
<section class="card draft" id="a-{k}">
  <div class=row><h2>{escape(p.get('company', ''))}{edited}</h2>
    <span class=muted>draft {idx + 1} of {total}</span></div>
  <p class=muted>{escape(a['kind'])} &middot; job <a href="/jobs/{escape(a['run_id'])}">{escape(a['run_id'])}</a>
    &middot; key <code>{k}</code> &middot; cost so far: {queued_s}Rs {run_cost_inr:.2f} for the job now</p>
  <div class=review>
    <div class=email>
      <p><b>To:</b> {escape(d.get('to', ''))}<br><b>Subject:</b> {escape(d.get('subject', ''))}</p>
      <pre class=mail>{body}</pre>
      <p class=muted>Calendar invite attached: {'yes' if d.get('ics') else 'no'}</p>
    </div>
    <div class=evidence>
      <h3>Buyer checks</h3>{buyer_html}
      <h3>Claims and sources</h3><ul class=claims>{claims}</ul>
      <h3>Route fit</h3>{fit_html}
    </div>
  </div>
  <div class=actionbar>
    <form method=post action="/actions/{k}/approve" class=inline>{hidden}
      <button class="btn success">{ICONS['check']}<span>Approve</span></button></form>
    <a class="btn" href="/actions/{k}/edit">{ICONS['edit']}<span>Edit</span></a>
    <form method=post action="/actions/{k}/reject" class=inline
      data-confirm="Reject this email? It will never be sent.">{hidden}
      <button class="btn danger">{ICONS['x']}<span>Reject</span></button></form>
    <span class=spacer></span>{nxt}
  </div>
</section>"""


CSS = r"""
:root{--bg:#f6f7fb;--bg2:#eef1f7;--fg:#111827;--muted:#6b7280;--card:#ffffff;--line:#e5e7ef;
--soft:#f3f5fa;--accent:#4f46e5;--accent2:#7c3aed;--accent-bg:#eef0ff;--ring:rgba(79,70,229,.28);
--good:#0f8a4f;--good-bg:#e7f7ee;--warn:#b26b00;--warn-bg:#fff4e0;--bad:#c62828;--bad-bg:#fdecec;
--info:#2563eb;--info-bg:#e8f0ff;--shadow:0 1px 2px rgba(17,24,39,.05),0 4px 16px rgba(17,24,39,.06);
--shadow-lg:0 10px 30px rgba(17,24,39,.12);--radius:14px;--ease:cubic-bezier(.2,.8,.2,1)}
[data-theme=dark]{--bg:#0d1017;--bg2:#121621;--fg:#e8eaf0;--muted:#9aa3b2;--card:#161a24;--line:#262c3a;
--soft:#1c2130;--accent:#8b85ff;--accent2:#b18cff;--accent-bg:#232447;--ring:rgba(139,133,255,.35);
--good:#4ade80;--good-bg:#12301f;--warn:#fbbf24;--warn-bg:#33270b;--bad:#f87171;--bad-bg:#3a1717;
--info:#7aa7ff;--info-bg:#17233d;--shadow:0 1px 2px rgba(0,0,0,.3);--shadow-lg:0 12px 32px rgba(0,0,0,.45)}
*{box-sizing:border-box}html{scroll-behavior:smooth;color-scheme:light}[data-theme=dark]{color-scheme:dark}
body{margin:0;background:radial-gradient(1200px 500px at 10% -10%,var(--accent-bg),transparent 60%),var(--bg);
color:var(--fg);font:15px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
transition:background-color .3s var(--ease),color .3s var(--ease);min-height:100vh}
::selection{background:var(--accent-bg)}
/* header */
header.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--card) 82%,transparent);
backdrop-filter:saturate(1.6) blur(12px);border-bottom:1px solid var(--line)}
.topin{max-width:1200px;margin:auto;display:flex;align-items:center;gap:22px;padding:10px 22px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;white-space:nowrap;
color:var(--fg);text-decoration:none}.brand small{display:block;font-weight:500;color:var(--muted);font-size:12px}
.logo{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:#fff;
background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 4px 12px var(--ring)}
nav{display:flex;gap:4px;flex:1;position:relative}
nav a{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:10px;color:var(--muted);
text-decoration:none;font-weight:600;transition:background .2s var(--ease),color .2s var(--ease)}
nav a:hover{background:var(--soft);color:var(--fg)}nav a.on{background:var(--accent-bg);color:var(--accent)}
.count{background:var(--bad);color:#fff;border-radius:10px;font-size:11px;padding:0 7px;line-height:18px;
animation:pop .4s var(--ease)}
.user{color:var(--muted);font-size:13px;display:flex;gap:10px;align-items:center;white-space:nowrap}
.iconbtn{width:36px;height:36px;border-radius:10px;border:1px solid var(--line);background:var(--card);
color:var(--fg);display:grid;place-items:center;cursor:pointer;transition:transform .15s var(--ease),
border-color .2s,background .2s}.iconbtn:hover{border-color:var(--accent);transform:translateY(-1px)}
.iconbtn:active{transform:scale(.94)}.iconbtn .sun{display:none}[data-theme=dark] .iconbtn .sun{display:block}
[data-theme=dark] .iconbtn .moon{display:none}
/* layout */
main{max-width:1200px;margin:auto;padding:26px 22px 60px}
h1{font-size:28px;letter-spacing:-.02em;margin:2px 0 18px}
h2{font-size:18px;margin:0;letter-spacing:-.01em}
h3{font-size:12px;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
h4{margin:14px 0 4px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px 24px;
margin:16px 0;box-shadow:var(--shadow);transition:box-shadow .25s var(--ease),border-color .25s,transform .25s var(--ease)}
.narrow{max-width:780px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.spacer{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 18px}
/* entrance animations */
@media (prefers-reduced-motion:no-preference){
 body:not(.polled) main>*{animation:rise .45s var(--ease) both}
 body:not(.polled) main>*:nth-child(2){animation-delay:.04s}body:not(.polled) main>*:nth-child(3){animation-delay:.08s}
 body:not(.polled) main>*:nth-child(4){animation-delay:.12s}body:not(.polled) main>*:nth-child(5){animation-delay:.16s}
 body:not(.polled) .plist>details{animation:rise .4s var(--ease) both}
 .plist>details:nth-child(2){animation-delay:.03s}.plist>details:nth-child(3){animation-delay:.06s}
 .plist>details:nth-child(4){animation-delay:.09s}.plist>details:nth-child(5){animation-delay:.12s}
 .plist>details:nth-child(n+6){animation-delay:.15s}
 body:not(.polled) .tracker li{animation:rise .4s var(--ease) both}
}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes pop{0%{transform:scale(.6);opacity:0}70%{transform:scale(1.12)}100%{transform:scale(1);opacity:1}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{background-position:-400px 0}100%{background-position:400px 0}}
@keyframes pulse{0%{box-shadow:0 0 0 0 var(--ring)}100%{box-shadow:0 0 0 10px transparent}}
/* hero */
.hero{padding:26px 28px;background:linear-gradient(180deg,var(--card),color-mix(in srgb,var(--card) 92%,var(--accent-bg)))}
.hero h2{font-size:22px}.hero textarea{font-size:17px;line-height:1.5;padding:14px 16px;margin:14px 0 8px;
min-height:96px;resize:vertical}
.preview{display:flex;flex-wrap:wrap;gap:8px;min-height:30px;margin:2px 0 10px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:13px;
font-weight:600;background:var(--accent-bg);color:var(--accent);animation:pop .3s var(--ease)}
.pill.miss{background:var(--warn-bg);color:var(--warn)}
.chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0 18px}
.chip{border:1px solid var(--line);background:var(--soft);border-radius:999px;padding:5px 13px;font:inherit;
font-size:13px;cursor:pointer;color:var(--fg);transition:all .2s var(--ease)}
.chip:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px);background:var(--accent-bg)}
kbd{font:12px ui-monospace,Consolas,monospace;border:1px solid var(--line);border-bottom-width:2px;
border-radius:5px;padding:1px 6px;background:var(--soft)}
/* forms */
label{display:block;font-weight:600;font-size:14px;margin-bottom:12px}
input,textarea{display:block;width:100%;font:inherit;padding:10px 12px;margin:6px 0 0;background:var(--bg);
color:var(--fg);border:1px solid var(--line);border-radius:10px;transition:border-color .2s,box-shadow .2s,background .2s}
input:hover,textarea:hover{border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
input:focus,textarea:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 4px var(--ring);background:var(--card)}
.mono{font:13px/1.55 ui-monospace,Consolas,monospace}
label.check{display:flex;gap:12px;align-items:flex-start;font-weight:400;margin:14px 0;cursor:pointer;
padding:12px 14px;border:1px solid var(--line);border-radius:12px;transition:border-color .2s,background .2s}
label.check:hover{border-color:var(--accent);background:var(--accent-bg)}
label.check input{width:18px;height:18px;margin-top:3px;accent-color:var(--accent)}
/* buttons */
.btn,button.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-weight:600;
font-size:14px;padding:9px 16px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);
cursor:pointer;text-decoration:none;position:relative;user-select:none;white-space:nowrap;
transition:transform .15s var(--ease),box-shadow .2s var(--ease),background .2s,border-color .2s,color .2s,filter .2s}
.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow);border-color:color-mix(in srgb,var(--accent) 50%,var(--line))}
.btn:active{transform:translateY(0) scale(.97);box-shadow:none}
.btn:focus-visible,.chip:focus-visible,.tile:focus-visible,nav a:focus-visible,.iconbtn:focus-visible{
outline:0;box-shadow:0 0 0 4px var(--ring)}
.btn.primary{color:#fff;border:0;background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 4px 14px var(--ring)}.btn.primary:hover{filter:brightness(1.07);box-shadow:0 8px 22px var(--ring)}
.btn.success{color:#fff;border:0;background:linear-gradient(135deg,#12a05c,#0f8a4f);box-shadow:0 4px 14px rgba(15,138,79,.25)}
.btn.success:hover{filter:brightness(1.06)}
.btn.danger{color:var(--bad);background:var(--card);border-color:color-mix(in srgb,var(--bad) 35%,var(--line))}
.btn.danger:hover{background:var(--bad-bg);border-color:var(--bad)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.ghost:hover{background:var(--soft);color:var(--fg);box-shadow:none}
.btn.lg{padding:12px 20px;font-size:15px;border-radius:12px}
.btn.loading{pointer-events:none;color:transparent!important}.btn.loading>*{visibility:hidden}
.btn.loading:after{content:"";position:absolute;width:16px;height:16px;border-radius:50%;border:2px solid currentColor;
border-color:#fff #fff transparent transparent;animation:spin .7s linear infinite;visibility:visible}
.btn.loading:not(.primary):not(.success):after{border-color:var(--accent) var(--accent) transparent transparent}
button.link{border:0;background:none;color:var(--accent);padding:0;font:inherit;cursor:pointer;font-weight:600}
.inline{display:inline}a{color:var(--accent);text-underline-offset:3px}
/* toasts + banners */
.toast{position:fixed;right:22px;bottom:22px;z-index:50;max-width:440px;display:flex;gap:12px;align-items:flex-start;
background:var(--fg);color:var(--bg);padding:12px 14px 12px 16px;border-radius:12px;box-shadow:var(--shadow-lg);
animation:toastin .4s var(--ease) both}.toast.out{animation:toastout .35s var(--ease) both}
.toast-x{background:none;border:0;color:inherit;opacity:.7;font-size:18px;line-height:1;cursor:pointer}
@keyframes toastin{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:none}}
@keyframes toastout{to{opacity:0;transform:translateY(12px)}}
.flash{border-radius:12px;padding:12px 16px;margin:12px 0;border:1px solid transparent}
.flash.info{background:var(--info-bg);border-color:color-mix(in srgb,var(--info) 25%,transparent)}
.flash.bad{background:var(--bad-bg);color:var(--bad)}
/* badges, tags */
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;border-radius:999px;padding:3px 11px;white-space:nowrap}
.badge:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.85}
.badge.good{background:var(--good-bg);color:var(--good)}.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.bad{background:var(--bad-bg);color:var(--bad)}.badge.info{background:var(--info-bg);color:var(--info)}
.badge.info:before{animation:pulse 1.4s infinite}.badge.muted{background:var(--soft);color:var(--muted)}
.tag{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);
border-radius:6px;padding:1px 6px;color:var(--muted)}
.muted{color:var(--muted);font-size:13px}.err{color:var(--bad)}.quote{font-size:18px;font-weight:500;margin:0 0 4px}
/* tables + job cards */
table{border-collapse:collapse;width:100%;font-size:14px}th{font-size:12px;text-transform:uppercase;color:var(--muted);
letter-spacing:.05em;font-weight:600}th,td{border-bottom:1px solid var(--line);padding:11px 10px;text-align:left;vertical-align:middle}
tr.link-row{cursor:pointer;transition:background .15s}tr.link-row:hover td{background:var(--soft)}
.jobgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;margin-top:10px}
.jobcard{display:block;color:var(--fg);text-decoration:none;margin:0}
.jobcard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.jobcard p{margin:10px 0 0}
.empty{text-align:center;padding:40px 20px;color:var(--muted)}.empty svg{width:40px;height:40px;opacity:.5}
/* job header + stats */
.jobtitle{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.stats{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:12px;margin:18px 0}
.stat{background:var(--soft);border-radius:12px;padding:12px 16px;display:flex;flex-direction:column;gap:2px}
.stat b{font-size:20px;letter-spacing:-.01em}.meter .bar{height:8px;background:var(--line);border-radius:4px;margin-top:8px;overflow:hidden}
.fill{height:100%;border-radius:4px;transition:width .8s var(--ease)}
.fill.good{background:linear-gradient(90deg,#16a34a,#22c55e)}.fill.warn{background:linear-gradient(90deg,#d97706,#f59e0b)}
.fill.bad{background:linear-gradient(90deg,#dc2626,#ef4444)}
/* tracker */
.tracker{list-style:none;display:grid;grid-template-columns:repeat(10,1fr);padding:18px 10px;margin:16px 0;
background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.tracker li{position:relative;text-align:center;font-size:12px;padding:0 4px}
.tracker li:before{content:"";position:absolute;top:11px;left:-50%;width:100%;height:3px;background:var(--line);z-index:0;
border-radius:2px;transition:background .5s var(--ease)}
.tracker li:first-child:before{display:none}
.tracker li.done:before,.tracker li.current:before,.tracker li.wait:before{background:linear-gradient(90deg,var(--good),var(--good))}
.dot{position:relative;z-index:1;display:grid;place-items:center;width:24px;height:24px;margin:0 auto 8px;border-radius:50%;
background:var(--card);border:2px solid var(--line);transition:all .35s var(--ease);font-size:12px;color:#fff}
.done .dot{background:var(--good);border-color:var(--good)}.done .dot:after{content:"\2713"}
.current .dot{border-color:var(--accent);animation:pulse 1.3s infinite}
.current .dot:after{content:"";width:10px;height:10px;border-radius:50%;border:2px solid var(--accent);
border-color:var(--accent) transparent var(--accent) transparent;animation:spin 1s linear infinite}
.wait .dot{background:var(--warn);border-color:var(--warn)}.wait .dot:after{content:"!";font-weight:800}
.failed .dot{background:var(--bad);border-color:var(--bad)}.failed .dot:after{content:"\00d7";font-weight:800}
.lbl{display:block;font-weight:600;line-height:1.3}.note{display:block;color:var(--muted);font-size:11px;margin-top:2px}
.current .lbl{color:var(--accent)}.todo .lbl{color:var(--muted)}
/* result tiles + prospects */
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}
.tile{display:flex;flex-direction:column;align-items:flex-start;min-width:118px;padding:12px 16px;border-radius:12px;
border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;cursor:pointer;
transition:transform .15s var(--ease),box-shadow .2s,border-color .2s,background .2s}
.tile:hover{transform:translateY(-2px);box-shadow:var(--shadow)}.tile:active{transform:scale(.97)}
.tile b{font-size:26px;line-height:1.1;letter-spacing:-.02em}.tile span{font-size:12px;color:var(--muted);font-weight:600}
.tile.good b{color:var(--good)}.tile.warn b{color:var(--warn)}.tile.bad b{color:var(--bad)}
.tile.on{border-color:var(--accent);background:var(--accent-bg);box-shadow:0 0 0 3px var(--ring)}
details.prospect{border:1px solid var(--line);border-radius:12px;margin:8px 0;background:var(--card);
transition:border-color .2s,box-shadow .2s;overflow:hidden}
details.prospect:hover{border-color:color-mix(in srgb,var(--accent) 35%,var(--line))}
details.prospect[open]{box-shadow:var(--shadow)}
details.prospect>summary{list-style:none;display:grid;grid-template-columns:minmax(180px,1.2fr) auto 3fr 18px;gap:14px;
align-items:center;padding:13px 16px;cursor:pointer}
details.prospect>summary::-webkit-details-marker{display:none}
details.prospect>summary:after{content:"";width:8px;height:8px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
transform:rotate(45deg);transition:transform .25s var(--ease);justify-self:end}
details.prospect[open]>summary:after{transform:rotate(225deg)}
details.prospect[open]>summary{border-bottom:1px solid var(--line);background:var(--soft)}
.co .muted{display:block}.pdetail{padding:6px 20px 16px}.hidden{display:none!important}
ul.claims{list-style:none;padding:0;margin:0}li.claim{margin:8px 0;padding:8px 10px;border-radius:10px;transition:background .3s var(--ease)}
li.claim.hl{background:var(--warn-bg);box-shadow:inset 3px 0 0 var(--warn)}.num{font-weight:800;color:var(--accent)}
ul.src{list-style:none;padding-left:10px;font-size:13px;margin:4px 0}
blockquote{margin:4px 0 6px;padding:6px 12px;border-left:3px solid var(--line);color:var(--muted);font-size:12.5px;
overflow-wrap:anywhere;background:var(--soft);border-radius:0 8px 8px 0}code{font-size:12px}
/* skeleton */
.skeleton .sk{background:linear-gradient(90deg,var(--soft) 0,var(--line) 50%,var(--soft) 100%);background-size:800px 100%;
animation:shimmer 1.4s linear infinite;border-radius:10px}
.sk-title{height:22px;width:180px;margin-bottom:16px}.sk-tiles{display:flex;gap:10px;margin-bottom:16px}
.sk-tiles .sk{height:62px;width:118px}.sk-row{height:48px;margin:8px 0}
/* draft review */
.review{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:24px;margin-top:12px}
pre.mail{white-space:pre-wrap;background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
font:14px/1.65 inherit;margin:10px 0}
a.cite{font-weight:700;text-decoration:none;background:var(--accent-bg);color:var(--accent);border-radius:5px;padding:0 4px;
transition:background .2s,color .2s}a.cite:hover{background:var(--accent);color:#fff}
.evidence{border-left:1px solid var(--line);padding-left:22px;max-height:660px;overflow:auto;scrollbar-width:thin}
ul.checks{list-style:none;padding:0;margin:8px 0}ul.checks li{display:flex;gap:10px;align-items:center;margin:6px 0}
ul.checks span{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;font-size:12px;font-weight:800}
ul.checks .ok span{background:var(--good-bg);color:var(--good)}ul.checks .warn span{background:var(--warn-bg);color:var(--warn)}
ul.checks .bad span{background:var(--bad-bg);color:var(--bad)}ul.checks .bad{color:var(--bad)}
.actionbar{position:sticky;bottom:0;background:color-mix(in srgb,var(--card) 90%,transparent);backdrop-filter:blur(8px);
border-top:1px solid var(--line);margin:18px -24px -20px;padding:12px 24px;border-radius:0 0 var(--radius) var(--radius);
display:flex;gap:10px;align-items:center}
.draft:target{animation:flashring 1.2s var(--ease)}
@keyframes flashring{0%{box-shadow:0 0 0 0 var(--ring)}40%{box-shadow:0 0 0 6px var(--ring)}100%{box-shadow:var(--shadow)}}
/* log */
.logbox>summary{font-weight:600}ul.log{list-style:none;padding:0;font-size:13px;max-height:320px;overflow:auto}
ul.log li{padding:3px 0;border-bottom:1px dashed var(--line)}
.lvl{font-size:10px;font-weight:700;text-transform:uppercase;border-radius:4px;padding:1px 5px;background:var(--soft)}
.lvl.error{color:var(--bad)}.lvl.warn{color:var(--warn)}ul.sent li{margin:6px 0}summary{cursor:pointer}
/* modal */
dialog.modal{border:0;border-radius:16px;padding:0;max-width:440px;width:calc(100% - 40px);background:var(--card);color:var(--fg);
box-shadow:var(--shadow-lg)}dialog.modal::backdrop{background:rgba(10,12,20,.45);backdrop-filter:blur(3px)}
dialog.modal[open]{animation:pop .3s var(--ease)}.modal-body{padding:22px 24px 8px;font-size:15px}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;padding:14px 20px 18px}
@media (max-width:860px){.review{grid-template-columns:1fr}.evidence{border:0;padding:0;max-height:none}
.stats{grid-template-columns:1fr 1fr}.tracker{grid-template-columns:repeat(5,1fr);row-gap:18px}
details.prospect>summary{grid-template-columns:1fr auto 18px}.why{grid-column:1/-1}.topin{flex-wrap:wrap}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = r"""
(() => {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  // theme
  const themeBtn = $('#theme');
  if (themeBtn) themeBtn.addEventListener('click', () => {
    const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = t; localStorage.setItem('theme', t);
  });
  // toasts
  const hideToast = t => { t.classList.add('out'); setTimeout(() => t.remove(), 350); };
  const initToasts = (root = document) => $$('.toast', root).forEach(t => {
    if (t.dataset.init) return; t.dataset.init = 1;
    t.querySelector('.toast-x')?.addEventListener('click', () => hideToast(t));
    setTimeout(() => hideToast(t), 6000);
    if (location.search.includes('msg=')) history.replaceState(null, '', location.pathname + location.hash);
  });
  // count-up numbers
  const countUp = (root = document) => $$('[data-count]', root).forEach(el => {
    if (el.dataset.done) return; el.dataset.done = 1;
    const n = +el.dataset.count; if (!n || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const t0 = performance.now(), d = 600;
    const step = t => { const k = Math.min(1, (t - t0) / d); el.textContent = Math.round(n * (1 - Math.pow(1 - k, 3)));
      if (k < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  });
  // confirm modal for forms with data-confirm
  const modal = $('#confirm');
  document.addEventListener('submit', e => {
    const f = e.target;
    if (f.method === 'dialog') return;
    if (f.dataset.confirm && !f.dataset.ok && modal) {
      e.preventDefault();
      $('.modal-body', modal).textContent = f.dataset.confirm;
      const yes = $('.modal-yes', modal);
      yes.className = 'btn modal-yes ' + (f.querySelector('.danger') ? 'danger' : 'primary');
      modal.showModal();
      modal.onclose = () => { if (modal.returnValue === 'yes') { f.dataset.ok = 1; f.requestSubmit(); } };
      return;
    }
    const b = e.submitter || f.querySelector('button:not([type=button])');
    if (b && !b.classList.contains('link')) { b.classList.add('loading'); }
  });
  // chips + live preview of what the parser understands
  const prompt = $('#prompt'), prev = $('#preview');
  let timer;
  const preview = () => {
    if (!prompt || !prev) return; const q = prompt.value.trim();
    if (q.length < 4) { prev.innerHTML = ''; return; }
    fetch('/jobs/preview?q=' + encodeURIComponent(q)).then(r => r.ok ? r.json() : null).then(j => {
      if (!j) return; const pill = (t, miss) => `<span class="pill${miss ? ' miss' : ''}">${t}</span>`;
      prev.innerHTML = (j.route.length >= 2 ? pill('Lane: ' + j.route[0] + ' &rarr; ' + j.route[1]) : pill('Lane: not found yet', 1))
        + pill(j.meetings + ' meetings') + (j.trucks != null ? pill(j.trucks + ' spare trucks/week') : pill('Trucks: from client records', 1));
    }).catch(() => {});
  };
  if (prompt) {
    prompt.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(preview, 250);
      prompt.style.height = 'auto'; prompt.style.height = Math.min(260, prompt.scrollHeight + 2) + 'px'; });
    prompt.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) prompt.form.requestSubmit(); });
  }
  document.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (chip && prompt) { prompt.value = chip.dataset.example; prompt.focus(); preview(); }
    const tile = e.target.closest('.tile');
    if (tile) {
      const f = tile.dataset.filter;
      $$('.tile').forEach(t => t.classList.toggle('on', t === tile && f !== ''));
      $$('details.prospect').forEach(d => d.classList.toggle('hidden', f !== '' && d.dataset.status !== f));
    }
    const cite = e.target.closest('a.cite');
    if (cite) { e.preventDefault(); const x = document.getElementById(cite.dataset.c);
      if (x) { x.scrollIntoView({behavior: 'smooth', block: 'center'}); x.classList.add('hl'); setTimeout(() => x.classList.remove('hl'), 1600); } }
  });
  document.addEventListener('mouseover', e => { const c = e.target.closest('a.cite');
    if (c) document.getElementById(c.dataset.c)?.classList.add('hl'); });
  document.addEventListener('mouseout', e => { const c = e.target.closest('a.cite');
    if (c) document.getElementById(c.dataset.c)?.classList.remove('hl'); });
  // smooth open/close for company rows
  document.addEventListener('click', e => {
    const s = e.target.closest('details.prospect > summary'); if (!s) return;
    const d = s.parentElement, body = d.querySelector('.pdetail');
    if (!body || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    e.preventDefault();
    if (d.open) {
      body.animate([{height: body.offsetHeight + 'px', opacity: 1}, {height: '0px', opacity: 0}],
        {duration: 220, easing: 'ease'}).onfinish = () => { d.open = false; };
    } else {
      d.open = true;
      body.animate([{height: '0px', opacity: 0}, {height: body.offsetHeight + 'px', opacity: 1}], {duration: 260, easing: 'ease'});
    }
  });
  // live job page: refresh the content in place, no page reload
  const live = () => {
    const box = $('#live'); if (!box || !box.dataset.poll) return;
    setTimeout(() => fetch(location.pathname, {headers: {'X-Partial': '1'}}).then(r => r.text()).then(html => {
      const doc = new DOMParser().parseFromString(html, 'text/html'), next = doc.querySelector('#live');
      if (!next) return;
      const openLog = $('#live details.logbox')?.open, y = scrollY;
      document.body.classList.add('polled'); box.replaceWith(next); if (openLog !== undefined) { const l = $('#live details.logbox'); if (l) l.open = openLog; }
      scrollTo(0, y); countUp(next); initToasts(next); live();
    }).catch(() => live()), 2500);
  };
  initToasts(); countUp(); live();
})();
"""


def layout(title: str, body: str, name: str = "", tab: str = "", pending: int = 0,
           client: str = "", head: str = "") -> str:
    nav = ""
    if name:
        def item(key: str, href: str, icon: str, label: str) -> str:
            return f"<a href='{href}' class='{'on' if tab == key else ''}'>{ICONS[icon]}{label}</a>"
        count = f"<span class=count>{pending}</span>" if pending else ""
        nav = (f"<nav>{item('new', '/', 'plus', 'New job')}{item('jobs', '/jobs', 'list', 'Jobs')}"
               f"{item('approvals', '/approvals', 'inbox', 'Approvals' + count)}</nav>"
               f"<div class=user><span>{escape(name)}</span><form method=post action=/logout class=inline>"
               f"<button class=link>Log out</button></form>")
    else:
        nav = "<nav></nav><div class=user>"
    theme = (f"<button type=button id=theme class=iconbtn title='Light / dark' aria-label='Toggle theme'>"
             f"<span class=moon>{ICONS['moon']}</span><span class=sun>{ICONS['sun']}</span></button></div>")
    sub = f"<small>for {escape(client)}</small>" if client else "<small>outbound meetings for a 3PL</small>"
    return f"""<!doctype html><html lang=en data-theme=light><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">{head}
<script>document.documentElement.dataset.theme=localStorage.getItem('theme')||'light'</script>
<title>{escape(title)} &middot; SDR service</title><style>{CSS}</style></head>
<body><header class=top><div class=topin><a class=brand href='/'><span class=logo>{ICONS['send']}</span>
<span>SDR service{sub}</span></a>{nav}{theme}</div></header>
<main><h1>{escape(title)}</h1>{body}</main>
<dialog id=confirm class=modal><form method=dialog><div class=modal-body></div>
<div class=modal-actions><button class="btn ghost" value=no>Cancel</button>
<button class="btn primary modal-yes" value=yes>Confirm</button></div></form></dialog>
<script>{JS}</script></body></html>"""


# kept for older imports
def page(title: str, body: str) -> str:
    return layout(title, body)
