"""Browser app: type a request, confirm what was understood, watch the job run, review
every company and draft with its sources, approve/edit/reject, and send approved emails.

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
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from datetime import date, datetime

from ..config import env, settings, usd_to_inr
from ..ledger import Ledger
from ..steps.s01_intake import parse_request
from .jobs import JobRunner, new_job_id

COOKIE = "svc_approver"
MAX_BODY = 10_000
_MARKER = re.compile(r"\[(\d+)\]")


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
        dest = next_ if re.fullmatch(r"/(jobs/[\w.-]+)?", next_ or "") else "/"
        return RedirectResponse(f"{dest}?msg={quote(msg)}", status_code=303)

    def job_row(job_id: str) -> dict:
        with lock:
            run = led.get_run(job_id)
            cost = usd_to_inr(led.run_cost(job_id))
        req = jobs.request(job_id)
        res = jobs.result(job_id)
        return {"id": job_id, "status": jobs.status(job_id, run), "cost": cost,
                "text": req.get("text", ""), "route": req.get("route") or [],
                "mode": jobs.meta(job_id).get("mode") or settings()["sources"]["mode"],
                "result": res, "created": jobs.meta(job_id).get("created")}

    # ---- routes ----------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "login_enabled": bool(pw)}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(error: str = "") -> str:
        note = ("" if pw else "<p class=err>APPROVER_PASSWORD is not set in .env: "
                "login is disabled.</p>")
        err = f"<p class=err>{escape(error)}</p>" if error else ""
        return page("Log in", f"""{note}{err}
<form method=post action=/login class=card>
  <label>Your name (recorded on every decision)<br>
    <input name=name required maxlength=80 autocomplete=name></label>
  <label>Password<br><input name=password type=password required></label>
  <button>Log in</button>
</form>""")

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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str = "") -> Response:
        name = approver(request)
        if name is None:
            return RedirectResponse("/login", status_code=303)
        with lock:
            pending = led.pending_approvals()
            run_cost = {r: usd_to_inr(led.run_cost(r)) for r in {a["run_id"] for a in pending}}
        token = csrf_for(request)
        cards = "".join(render_action(a, run_cost[a["run_id"]], token) for a in pending)
        flash = f"<p class=flash>{escape(msg)}</p>" if msg else ""
        empty = "<p class=muted>Nothing waiting for approval.</p>" if not pending else ""
        busy = jobs.running_job()
        busy_html = (f"<p class=flash>Job <a href='/jobs/{escape(busy)}'>{escape(busy)}</a> is "
                     "running. A new job can start when it finishes.</p>" if busy else "")
        rows = "".join(render_job_row(job_row(j)) for j in jobs.job_ids()[:15])
        jobs_html = (f"<table class=jobs><tr><th>Job</th><th>Request</th><th>Status</th>"
                     f"<th>Drafts</th><th>Cost</th></tr>{rows}</table>" if rows
                     else "<p class=muted>No jobs yet.</p>")
        return HTMLResponse(page("SDR service", f"""
<div class=bar>Logged in as <b>{escape(name)}</b>
  <form method=post action=/logout class=inline><button class=link>log out</button></form></div>
{flash}{busy_html}
<form method=post action=/jobs/parse class=card>
  <input type=hidden name=csrf value="{token}">
  <label><b>What should the service do?</b><br>
  <textarea name=prompt rows=3 required maxlength=1000
   placeholder="Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"></textarea></label>
  <button class=ok>Next: check what I understood</button>
</form>
<h2>Jobs</h2>{jobs_html}
<h2>Pending approvals ({len(pending)})</h2>
<p class=muted>Approving only marks an email approved. It is sent by the job's send step:
press <b>Send approved</b> on the job page (or re-run the same request).</p>
{empty}{cards}"""))

    # ---- jobs: prompt -> confirm -> run -> result -------------------------
    @app.post("/jobs/parse", response_class=HTMLResponse)
    def parse(request: Request, csrf: str = Form(...), prompt: str = Form(...)) -> Response:
        require(request, csrf)
        prompt = " ".join(prompt.split())[:1000]
        job = parse_request({"text": prompt, "request_date": date.today().isoformat()})
        route = job.route_filter + ["", ""]
        warn = ("" if len(job.route_filter) >= 2 else
                "<p class=err>I could not find two cities for the lane in your request. "
                "Fill them in below.</p>")
        default_budget = settings()["limits"]["max_cost_inr"]
        live = settings()["sources"]["mode"] == "live"
        return HTMLResponse(page("Check the request", f"""{warn}
<form method=post action=/jobs class=card>
  <input type=hidden name=csrf value="{csrf_for(request)}">
  <input type=hidden name=prompt value="{escape(prompt)}">
  <p class=muted>Your request: <i>{escape(prompt)}</i></p>
  <div class=grid>
    <label>Lane from<br><input name=from_city value="{escape(route[0].title())}" required></label>
    <label>Lane to<br><input name=to_city value="{escape(route[1].title())}" required></label>
    <label>Meetings wanted<br><input name=meetings type=number min=1 max=50
      value="{job.meetings_wanted}" required></label>
    <label>Spare trucks per week<br><input name=trucks type=number min=0 max=1000
      value="{job.spare_trucks_per_week if job.spare_trucks_per_week is not None else ''}"
      placeholder="from client records"></label>
    <label>Request date (trigger window ends here)<br><input name=request_date type=date
      value="{job.request_date.isoformat()}" required></label>
    <label>Cost limit (Rs)<br><input name=budget_inr type=number min=1 step=1
      value="{int(default_budget)}" required></label>
  </div>
  <label class=check><input type=checkbox name=rehearsal value=1 {'' if live else 'checked'}>
    Rehearsal: use test data (no Crustdata credits)</label>
  <p class=muted>A live job takes about 5-10 minutes (the data provider allows 5 requests a
    minute). You can leave this page; progress is saved.</p>
  <button class=ok>Run the job</button> <a href="/">cancel</a>
</form>"""))

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
                "SELECT at, level, msg FROM events WHERE run_id=? ORDER BY id DESC LIMIT 25",
                (job_id,)).fetchall()
            acts = led.actions_for_run(job_id)
            run_cost = usd_to_inr(led.run_cost(job_id))
        token = csrf_for(request)
        running = j["status"] == "running"
        refresh = "<meta http-equiv=refresh content=4>" if running else ""
        pending = [a for a in acts if a["status"] == "pending_approval"]
        approved = [a for a in acts if a["status"] in ("approved", "started", "failed")]
        sent = [a for a in acts if a["status"] == "done"]
        buttons = []
        if running:
            buttons.append(form_button(f"/jobs/{job_id}/stop", token, "Stop (hard kill)",
                                       "bad", "Kill this job now? Run it again to resume."))
        else:
            if j["status"] in ("interrupted", "partial", "stopped_limit", "failed"):
                buttons.append(form_button(f"/jobs/{job_id}/resume", token,
                                           "Resume from where it stopped", "ok"))
            if approved:
                buttons.append(form_button(f"/jobs/{job_id}/send", token,
                                           f"Send approved ({len(approved)})", "ok",
                                           f"Send {len(approved)} approved email(s) with "
                                           "calendar invites? Each is sent once."))
        log = "".join(f"<li><span class=muted>{datetime.fromtimestamp(at):%H:%M:%S}</span> "
                      f"<span class='lvl {escape(lv)}'>{escape(lv)}</span> {escape(m)}</li>"
                      for at, lv, m in reversed(events))
        res = j["result"] if not running else None
        result_html = render_result(res) if res else (
            "<p class=muted>Working... this page refreshes by itself.</p>" if running else
            f"<pre>{escape(jobs.log_tail(job_id))}</pre>" if j["status"] == "failed" else "")
        cards = "".join(render_action(a, run_cost, token, next_=f"/jobs/{job_id}")
                        for a in pending)
        sent_html = "".join(f"<li>{escape(a['payload'].get('company', ''))}: sent "
                            f"(approved by {escape(a.get('approved_by') or '?')})</li>"
                            for a in sent)
        flash = f"<p class=flash>{escape(msg)}</p>" if msg else ""
        return HTMLResponse(page(f"Job {job_id}", f"""{refresh}
<p><a href="/">&larr; all jobs</a></p>{flash}
<section class=card>
  <p><b>Request:</b> {escape(j['text'])}</p>
  <p>Status: <span class="st {escape(j['status'])}">{escape(j['status'])}</span> ·
    data: {'test data (rehearsal)' if j['mode'] == 'fixtures' else 'live'} ·
    cost so far: <b>Rs {j['cost']:.2f}</b> ·
    approved waiting to send: {len(approved)} · sent: {len(sent)}</p>
  <div class=actions>{''.join(buttons)}</div>
</section>
{result_html}
{"<h2>Waiting for your approval (" + str(len(pending)) + ")</h2>" + cards if pending else ""}
{"<h2>Sent</h2><ul>" + sent_html + "</ul>" if sent else ""}
<details {'open' if running else ''}><summary>Progress log</summary><ul class=log>{log}</ul>
</details>"""))

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

    @app.post("/actions/{key}/approve")
    def approve(key: str, request: Request, csrf: str = Form(...),
                next: str = Form("/")) -> Response:  # noqa: A002
        name = require(request, csrf)
        with lock:
            a = pending_action(key)
            led.decide(key, True, by=name)
        return back(f"Approved: {a['payload'].get('company', key)} (by {name}). "
                    "Press 'Send approved' on the job page to send it.", next)

    @app.post("/actions/{key}/reject")
    def reject(key: str, request: Request, csrf: str = Form(...),
               next: str = Form("/")) -> Response:  # noqa: A002
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
        err = f"<p class=err>{escape(error)}</p>" if error else ""
        claims = "".join(render_claim(i, c) for i, c in enumerate(d.get("claims", []), 1))
        return HTMLResponse(page(f"Edit: {a['payload'].get('company', '')}", f"""{err}
<form method=post action="/actions/{escape(key)}/edit" class=card>
  <input type=hidden name=csrf value="{csrf_for(request)}">
  <p><b>To:</b> {escape(d.get('to', ''))} <span class=muted>(recipient and invite
    cannot be edited here)</span></p>
  <label>Subject<br><input name=subject value="{escape(d.get('subject', ''))}"
    required maxlength=200></label>
  <label>Body<br><textarea name=body rows=18 required>{escape(d.get('body', ''))}</textarea>
  </label>
  <p class=muted>Keep the unsubscribe line. Do not add facts: every [n] must point at
    one of the sourced claims below.</p>
  <button>Save edit (stays pending)</button> <a href="/">cancel</a>
</form>
<h3>Sourced claims you may cite</h3><ul class=claims>{claims}</ul>"""))

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
                    "still pending approval.")

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


# ---- rendering ---------------------------------------------------------------

def _link(url: str) -> str:
    u = escape(url)
    if url.startswith(("http://", "https://")):
        return f'<a href="{u}" target=_blank rel="noopener noreferrer">{u}</a>'
    return f"<code>{u}</code>"


def render_source(s: dict) -> str:
    bits = [f"<span class=kind>{escape(s.get('kind', ''))}</span>", _link(s.get("url", ""))]
    if s.get("field_path"):
        bits.append(f"<code>{escape(s['field_path'])}</code>")
    date = s.get("record_date")
    bits.append(f"record date <b>{escape(date)}</b>" if date
                else "<span class=err>no record date</span>")
    return (f"<li>{' · '.join(bits)}<blockquote>{escape(s.get('excerpt', ''))}"
            "</blockquote></li>")


def render_claim(i: int | None, fact: dict) -> str:
    srcs = "".join(render_source(s) for s in fact.get("sources", []))
    label = f"<b>[{i}]</b> " if i is not None else ""
    return f"<li>{label}{escape(fact.get('claim', ''))}<ul class=src>{srcs}</ul></li>"


def render_action(a: dict, run_cost_inr: float, csrf: str, next_: str = "/") -> str:
    p, key = a["payload"], a["key"]
    d = p.get("draft", {})
    k = escape(key)
    claims = "".join(render_claim(i, c) for i, c in enumerate(d.get("claims", []), 1))

    fit = p.get("route_fit_detail")
    if fit:
        fit_html = (f"<p><b>{escape(fit['level'])}</b> fit, score {fit['score']:.2f} "
                    f"<span class=muted>(rule estimate)</span>: {escape(fit['reason'])}</p>"
                    f"<ul>{''.join(render_claim(None, f) for f in fit.get('facts', []))}</ul>")
    else:
        fit_html = f"<p>{escape(str(p.get('route_fit', '')))}</p>"

    b = p.get("buyer")
    if b:
        id_ok = b.get("employer_id") and b.get("employer_id") == b.get("company_id")
        id_note = ("<span class=ok>employer id matches company</span>" if id_ok
                   else "<span class=err>employer id does not match the company id: "
                        "check before approving</span>")
        role = escape(b.get("role_start") or "unknown")
        left = (f" · <span class=err>left {escape(b['role_end'])}</span>"
                if b.get("role_end") else "")
        buyer_html = (f"<p><b>{escape(b['name'])}</b>, {escape(b['title'])} at "
                      f"{escape(b.get('employer_name') or '?')} · in role since {role}{left}"
                      f" · {escape(b.get('business_email') or 'no business email')} · "
                      f"{id_note}</p><ul>"
                      f"{''.join(render_claim(None, f) for f in b.get('facts', []))}</ul>")
    else:
        buyer_html = "<p class=muted>Buyer evidence not in this payload (queued by an " \
                     "older version of step 9).</p>"

    edited = (f" <span class=tag>edited by {escape(d['edited_by'])}</span>"
              if d.get("edited_by") else "")
    queued = p.get("cost_inr_so_far")
    queued_s = f"Rs {queued:.2f} when queued · " if isinstance(queued, int | float) else ""
    return f"""
<section class=card id="a-{k}">
  <h2>{escape(p.get('company', ''))}{edited}</h2>
  <p class=muted>{escape(a['kind'])} · job <code>{escape(a['run_id'])}</code> ·
    key <code>{k}</code> · cost so far: {queued_s}Rs {run_cost_inr:.2f} for the job now</p>
  <h3>Email</h3>
  <p><b>To:</b> {escape(d.get('to', ''))}<br><b>Subject:</b> {escape(d.get('subject', ''))}</p>
  <pre>{escape(d.get('body', ''))}</pre>
  <p class=muted>Calendar invite attached: {'yes' if d.get('ics') else 'no'}</p>
  <h3>Claims and sources</h3><ul class=claims>{claims}</ul>
  <h3>Route fit</h3>{fit_html}
  <h3>Buyer evidence</h3>{buyer_html}
  <div class=actions>
    <form method=post action="/actions/{k}/approve" class=inline>
      <input type=hidden name=csrf value="{csrf}"><input type=hidden name=next
      value="{escape(next_)}"><button class=ok>Approve</button></form>
    <a class=button href="/actions/{k}/edit">Edit</a>
    <form method=post action="/actions/{k}/reject" class=inline
      onsubmit="return confirm('Reject this email? It will never be sent.')">
      <input type=hidden name=csrf value="{csrf}"><input type=hidden name=next
      value="{escape(next_)}"><button class=bad>Reject</button></form>
  </div>
</section>"""


def form_button(action: str, csrf: str, label: str, cls: str = "", confirm: str = "") -> str:
    onsubmit = f" onsubmit=\"return confirm('{escape(confirm)}')\"" if confirm else ""
    return (f'<form method=post action="{escape(action)}" class=inline{onsubmit}>'
            f'<input type=hidden name=csrf value="{csrf}">'
            f'<button class="{cls}">{escape(label)}</button></form>')


STATUS_ORDER = ["awaiting_approval", "sent", "rejected", "held", "cannot_verify", "excluded",
                "dropped", "active"]


def render_job_row(j: dict) -> str:
    res = j["result"] or {}
    drafts = sum(1 for p in res.get("prospects", [])
                 if p.get("status") in ("awaiting_approval", "sent"))
    when = (datetime.fromtimestamp(j["created"]).strftime("%d %b %H:%M")
            if j.get("created") else "")
    mode = " <span class=tag>test data</span>" if j["mode"] == "fixtures" else ""
    return (f"<tr><td><a href='/jobs/{escape(j['id'])}'>{escape(when or j['id'])}</a>{mode}</td>"
            f"<td>{escape(j['text'][:90])}</td>"
            f"<td><span class='st {escape(j['status'])}'>{escape(j['status'])}</span></td>"
            f"<td>{drafts if res else ''}</td><td>Rs {j['cost']:.0f}</td></tr>")


def render_result(res: dict) -> str:
    ps = sorted(res.get("prospects", []),
                key=lambda p: STATUS_ORDER.index(p.get("status"))
                if p.get("status") in STATUS_ORDER else 99)
    counts: dict[str, int] = {}
    for p in ps:
        counts[p.get("status", "?")] = counts.get(p.get("status", "?"), 0) + 1
    summary = " · ".join(f"{v} {escape(k.replace('_', ' '))}" for k, v in counts.items())
    rows = []
    for p in ps:
        c = p.get("company") or {}
        t = (p.get("triggers") or [None])[0]
        trig = ""
        if t:
            src = (t.get("fact") or {}).get("sources") or [{}]
            trig = (f"{escape(t.get('summary', ''))} <span class=muted>"
                    f"({escape(str(t.get('event_date') or 'undated'))})</span> "
                    + (_link(src[0].get("url", "")) if src[0].get("url", "").startswith("http")
                       else ""))
        fit = escape((p.get("route_fit") or {}).get("reason") or "")
        b = p.get("buyer")
        buyer = f"{escape(b['name'])}, {escape(b['title'])}" if b else ""
        cv = "; ".join(escape(x.get("missing", "")) for x in p.get("cannot_verify") or [])
        rows.append(
            f"<tr><td><b>{escape(c.get('name', ''))}</b><br><span class=muted>"
            f"{escape(c.get('industry') or '')}</span></td>"
            f"<td><span class='st {escape(p.get('status', ''))}'>"
            f"{escape((p.get('status') or '').replace('_', ' '))}</span><br>"
            f"<span class=muted>{escape(p.get('reason') or '')}</span></td>"
            f"<td>{trig}</td><td>{fit}</td><td>{buyer}</td>"
            f"<td class=muted>{cv}</td></tr>")
    notes = "".join(f"<li>{escape(n)}</li>" for n in res.get("notes", []))
    return f"""<section class=card><h2>Result: {escape(res.get('status', ''))}</h2>
<p>{summary or 'No companies found.'} · cost Rs {res.get('cost_inr', 0):.2f}</p>
{"<ul class=muted>" + notes + "</ul>" if notes else ""}
<div class=scroll><table class=result><tr><th>Company</th><th>Status and reason</th>
<th>Trigger (source)</th><th>Route fit (estimate)</th><th>Buyer</th><th>Cannot verify</th></tr>
{''.join(rows)}</table></div></section>"""


CSS = """
:root{--bg:#f6f7f9;--fg:#1d2330;--muted:#667085;--card:#fff;--line:#e3e6eb;
--ok:#137333;--bad:#b3261e;--accent:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e8eb;--muted:#9aa3af;
--card:#1d2128;--line:#2e333b;--ok:#6ccf8e;--bad:#f28b82;--accent:#8ab4f8}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:900px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px 20px;
margin:16px 0}h2{margin:0 0 4px}h3{margin:18px 0 6px;font-size:15px}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);padding:12px;
border-radius:6px;font:13px/1.5 ui-monospace,Menlo,monospace}
blockquote{margin:4px 0 8px;padding:4px 10px;border-left:3px solid var(--line);
color:var(--muted);font-size:13px}ul.src{list-style:none;padding-left:8px;font-size:13px}ul.claims{list-style:none;padding-left:0}
.muted{color:var(--muted);font-size:13px}.err{color:var(--bad)}.ok{color:var(--ok)}
.flash{background:var(--card);border-left:4px solid var(--accent);padding:8px 12px}
.kind,.tag{font-size:11px;text-transform:uppercase;border:1px solid var(--line);
border-radius:4px;padding:0 4px}a{color:var(--accent);overflow-wrap:anywhere}
.inline{display:inline}.actions{margin-top:16px;display:flex;gap:10px;align-items:center}
button,.button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none}
button.ok{background:var(--ok);color:#fff;border:0}button.bad{color:var(--bad)}
button.link{border:0;background:none;color:var(--accent);padding:0}
input,textarea{width:100%;font:inherit;padding:6px;margin:4px 0 12px;background:var(--bg);
color:var(--fg);border:1px solid var(--line);border-radius:6px}
textarea{font:13px/1.5 ui-monospace,Menlo,monospace}.bar{display:flex;gap:8px}
textarea[name=prompt]{font:15px/1.5 system-ui,sans-serif}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 16px}
label.check{display:flex;gap:8px;align-items:center}label.check input{width:auto;margin:0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.scroll{overflow-x:auto}.st{font-size:12px;border-radius:10px;padding:1px 8px;
border:1px solid var(--line);white-space:nowrap}
.st.awaiting_approval,.st.done,.st.sent{color:var(--ok);border-color:var(--ok)}
.st.running{color:var(--accent);border-color:var(--accent)}
.st.interrupted,.st.partial,.st.stopped_limit,.st.failed,.st.cannot_verify{color:var(--bad);
border-color:var(--bad)}ul.log{list-style:none;padding-left:0;font-size:13px}
.lvl{font-size:11px;text-transform:uppercase}.lvl.error{color:var(--bad)}
details{margin:16px 0}summary{cursor:pointer}main{max-width:1150px}
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head>
<body><main><h1>{escape(title)}</h1>{body}</main></body></html>"""
