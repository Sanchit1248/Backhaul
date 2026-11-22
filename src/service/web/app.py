"""Approval page: one list of pending actions with full evidence + approve/edit/reject.

Rules this file keeps:
  * it only calls ledger.get_action / pending_approvals / decide / update_payload;
    it never imports the mailer or step 10, so nothing is ever sent from here;
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

from ..config import env, usd_to_inr
from ..ledger import Ledger

COOKIE = "svc_approver"
MAX_BODY = 10_000
_MARKER = re.compile(r"\[(\d+)\]")


def create_app(ledger: Ledger | None = None, password: str | None = None,
               session_secret: str | None = None) -> FastAPI:
    app = FastAPI(title="SDR approvals", docs_url=None, redoc_url=None, openapi_url=None)
    led = ledger or Ledger()
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

    def back(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

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
        empty = "<p>Nothing waiting for approval.</p>" if not pending else ""
        return HTMLResponse(page(f"Pending approvals ({len(pending)})", f"""
<div class=bar>Logged in as <b>{escape(name)}</b>
  <form method=post action=/logout class=inline><button class=link>log out</button></form></div>
<p class=muted>Approving only marks the action approved. Step 10 sends it on the next
pipeline run (<code>svc run</code> with the same request).</p>
{flash}{empty}{cards}"""))

    @app.post("/actions/{key}/approve")
    def approve(key: str, request: Request, csrf: str = Form(...)) -> Response:
        name = require(request, csrf)
        with lock:
            a = pending_action(key)
            led.decide(key, True, by=name)
        return back(f"Approved: {a['payload'].get('company', key)} (by {name}). "
                    "It will be sent on the next pipeline run.")

    @app.post("/actions/{key}/reject")
    def reject(key: str, request: Request, csrf: str = Form(...)) -> Response:
        name = require(request, csrf)
        with lock:
            a = pending_action(key)
            led.decide(key, False, by=name)
        return back(f"Rejected: {a['payload'].get('company', key)} (by {name}).")

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


def render_action(a: dict, run_cost_inr: float, csrf: str) -> str:
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
      <input type=hidden name=csrf value="{csrf}"><button class=ok>Approve</button></form>
    <a class=button href="/actions/{k}/edit">Edit</a>
    <form method=post action="/actions/{k}/reject" class=inline
      onsubmit="return confirm('Reject this email? It will never be sent.')">
      <input type=hidden name=csrf value="{csrf}"><button class=bad>Reject</button></form>
  </div>
</section>"""


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
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head>
<body><main><h1>{escape(title)}</h1>{body}</main></body></html>"""
