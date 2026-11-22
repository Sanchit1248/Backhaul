"""LaneLead pages on the approval app: /chat, /map, /memory and the request API.

Same login as the approval page. Nothing here sends: chat-started target searches run
the normal pipeline, whose drafts wait in the approval queue; sending stays in step 10
and only for actions a person approved.
"""
from __future__ import annotations

import hmac
import json
import math
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..chat import service as chat
from ..chat.memory import ConversationMemory, new_conv_id
from ..core.evidence import EvidenceStore
from ..core.memory import AccountMemory
from ..core.tasks import get_task, recent_tasks, run_task, start_task
from ..ledger import Ledger
from ..routes.cities import coords
from ..routes.sync import latest, sync


def register(app: FastAPI, *, led: Ledger, lock, approver, csrf_for, require,
             api_token: str) -> None:
    from .app import page  # late import: app.py imports this module

    def need_login(request: Request) -> str | None:
        return approver(request)

    # ---- chat ------------------------------------------------------------
    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, c: str = "") -> Response:
        name = need_login(request)
        if name is None:
            return RedirectResponse("/login", status_code=303)
        conv = c or new_conv_id()
        with lock:
            turns = ConversationMemory(led).turns(conv)
            rendered = [(t, chat.render(led, t)) for t in turns]
            store = EvidenceStore(led)
            html_turns = "".join(render_turn(t, r, store, csrf_for(request)) for t, r in rendered)
        busy = any(r["state"] == "running" for _, r in rendered)
        refresh = "<meta http-equiv=refresh content=3>" if busy else ""
        return HTMLResponse(page("Chat", f"""
<p class=muted>Ask about targets, churn, capacity, or follow up on a list. Every fact in an
answer links to its source. Target searches use paid data and wait for you to click Run.</p>
{html_turns}
<form method=post action=/chat/ask class=card>
  <input type=hidden name=csrf value="{csrf_for(request)}">
  <input type=hidden name=conv value="{escape(conv)}">
  <label>Question<br><input name=question required maxlength=500 autofocus
    placeholder="Find 5 targets on Pune to Mumbai in the last 30 days"></label>
  <button>Ask</button> <a href="/chat">new conversation</a>
</form>""", head=refresh))

    @app.post("/chat/ask")
    def chat_ask(request: Request, csrf: str = Form(...), conv: str = Form(...),
                 question: str = Form(...)) -> Response:
        name = require(request, csrf)
        with lock:
            chat.ask(led, conv, name, question)
        return RedirectResponse(f"/chat?c={quote(conv)}", status_code=303)

    @app.post("/chat/run/{turn_id}")
    def chat_run(turn_id: int, request: Request, csrf: str = Form(...)) -> Response:
        require(request, csrf)
        with lock:
            t = ConversationMemory(led).turn(turn_id)
            if t is None:
                raise HTTPException(404, "no such turn")
            chat.run(led, turn_id)
        return RedirectResponse(f"/chat?c={quote(t['conv_id'])}#t{turn_id}", status_code=303)

    # ---- map -------------------------------------------------------------
    @app.get("/map", response_class=HTMLResponse)
    def map_page(request: Request) -> Response:
        if need_login(request) is None:
            return RedirectResponse("/login", status_code=303)
        with lock:
            snap = latest(led)
            targets = next((t["result"] for t in recent_tasks(led, "find_targets", 5)
                            if t["status"] == "done"), None)
            churn = next((t["result"] for t in recent_tasks(led, "check_churn", 5)
                          if t["status"] == "done"), None)
        body = render_map(snap, targets, churn, csrf_for(request))
        return HTMLResponse(page("Routes map", body))

    @app.post("/map/sync")
    def map_sync(request: Request, csrf: str = Form(...)) -> Response:
        require(request, csrf)
        with lock:
            sync(led)
        return RedirectResponse("/map", status_code=303)

    # ---- memory ----------------------------------------------------------
    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request) -> Response:
        if need_login(request) is None:
            return RedirectResponse("/login", status_code=303)
        token = csrf_for(request)
        with lock:
            hist = AccountMemory(led).history()
            convs = ConversationMemory(led).conversations()
        rows = "".join(
            f"<tr><td>{escape(h['day'])}</td><td>{escape(h['company'])}</td>"
            f"<td>{escape(h['action'])}</td><td>{escape(h['outcome'] or '')}</td>"
            f"<td><code>{escape((h['job_id'] or '')[:24])}</code></td>"
            f"<td><form method=post action=/memory/company class=inline>"
            f"<input type=hidden name=csrf value='{token}'>"
            f"<input type=hidden name=key value='{escape(h['company_key'])}'>"
            f"<button class=link>forget company</button></form></td></tr>" for h in hist)
        crow = "".join(
            f"<tr><td><a href='/chat?c={escape(c['conv_id'])}'>{escape(c['conv_id'])}</a></td>"
            f"<td>{c['turns']}</td><td>{escape(c['first'][:80])}</td>"
            f"<td><form method=post action=/memory/conversation class=inline>"
            f"<input type=hidden name=csrf value='{token}'>"
            f"<input type=hidden name=conv value='{escape(c['conv_id'])}'>"
            f"<button class=link>delete</button></form></td></tr>" for c in convs)
        return HTMLResponse(page("What LaneLead remembers", f"""
<p class=muted>Account memory is company-level only (no people data). It holds targets we
already emailed (config <code>memory.*</code>). Deleting a company here means it can be
contacted again. People data in the outbox is purged after the retention period
(<code>svc purge</code>).</p>
<h3>Account memory ({len(hist)})</h3>
<table class=grid><tr><th>Date</th><th>Company</th><th>Action</th><th>Outcome</th><th>Job</th>
<th></th></tr>{rows or "<tr><td colspan=6>Nothing yet.</td></tr>"}</table>
<h3>Conversations ({len(convs)})</h3>
<table class=grid><tr><th>Conversation</th><th>Turns</th><th>First question</th><th></th></tr>
{crow or "<tr><td colspan=4>Nothing yet.</td></tr>"}</table>"""))

    @app.post("/memory/company")
    def forget_company(request: Request, csrf: str = Form(...), key: str = Form(...)) -> Response:
        require(request, csrf)
        with lock:
            AccountMemory(led).forget(key)
        return RedirectResponse("/memory", status_code=303)

    @app.post("/memory/conversation")
    def forget_conv(request: Request, csrf: str = Form(...), conv: str = Form(...)) -> Response:
        require(request, csrf)
        with lock:
            ConversationMemory(led).forget(conv)
        return RedirectResponse("/memory", status_code=303)

    # ---- request API -----------------------------------------------------
    def api_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if api_token and auth.startswith("Bearer ") and \
                hmac.compare_digest(auth[7:].encode(), api_token.encode()):
            return
        if approver(request) is not None:
            return
        raise HTTPException(401, "send Authorization: Bearer <LANELEAD_API_TOKEN> or log in")

    @app.post("/api/request")
    async def api_request(request: Request, wait: bool = True) -> Response:
        api_auth(request)
        try:
            raw = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(400, "body must be the request as JSON") from None
        if not isinstance(raw, dict):
            raise HTTPException(400, "body must be a JSON object")
        if not wait:
            with lock:
                tid = start_task(led, "job", {"request": raw})
            return JSONResponse({"task_id": tid, "status_url": f"/api/tasks/{tid}"}, 202)
        own = Ledger(led.db.execute("PRAGMA database_list").fetchone()[2])
        try:
            t = await _in_thread(lambda: run_task(own, "job", {"request": raw}))
        finally:
            own.close()
        return JSONResponse(t["result"])

    @app.get("/api/tasks/{tid}")
    def api_task(tid: str, request: Request) -> Response:
        api_auth(request)
        with lock:
            t = get_task(led, tid)
        if t is None:
            raise HTTPException(404, "no such task")
        return JSONResponse({k: t[k] for k in ("task_id", "type", "status", "result", "error")})


async def _in_thread(fn):
    import anyio

    return await anyio.to_thread.run_sync(fn)


# ---- rendering ---------------------------------------------------------------

def _link(url: str) -> str:
    u = escape(url)
    if url.startswith(("http://", "https://")):
        return f'<a href="{u}" target=_blank rel="noopener noreferrer">{u}</a>'
    return f"<code>{u}</code>"


def render_evidence(ids: list[str], store: EvidenceStore) -> str:
    items = []
    for eid in ids[:12]:
        f = store.get_fact(eid)
        if f is None:
            continue
        for s in f.sources[:3]:
            date = s.record_date.isoformat() if s.record_date else "no record date"
            items.append(f"<li>{_link(s.url)} · <b>{escape(date)}</b>"
                         f"<blockquote>{escape(s.excerpt[:300])}</blockquote></li>")
    more = f"<li class=muted>+{len(ids) - 12} more</li>" if len(ids) > 12 else ""
    return (f"<details class=ev><summary>sources ({len(ids)})</summary>"
            f"<ul class=src>{''.join(items)}{more}</ul></details>")


def render_lines(lines: list[dict], store: EvidenceStore) -> str:
    out = []
    for ln in lines:
        t = escape(ln["text"])
        if ln["kind"] == "heading":
            out.append(f"<h4>{t}</h4>")
        elif ln["kind"] == "link":
            out.append(f"<p><a href=\"{escape(ln['href'])}\">{t}</a></p>")
        elif ln["kind"] == "pre":
            out.append(f"<pre>{t}</pre>")
        elif ln["kind"] == "fact":
            out.append(f"<p>{t}</p>{render_evidence(ln['evidence_ids'], store)}")
        else:
            cls = " class=err" if ln["text"].startswith("cannot verify") else " class=muted"
            out.append(f"<p{cls}>{t}</p>")
    return "".join(out)


def render_turn(turn: dict, r: dict, store: EvidenceStore, csrf: str) -> str:
    p = turn["parsed"]
    head = (f"<div class=turn id=t{turn['turn_id']}><p class=q>{escape(turn['author'])}: "
            f"{escape(turn['question'])}</p>")
    if p.get("type"):
        head += (f"<p class=muted>Task: <code>{escape(p['type'])}</code> "
                 f"{escape(json.dumps(p['params']))} · {escape(p['summary'])} "
                 f"(parsed by {escape(p['parser'])})</p>")
    if r["state"] == "parsed":
        head += (f"<form method=post action=/chat/run/{turn['turn_id']} class=inline>"
                 f"<input type=hidden name=csrf value='{csrf}'><button class=ok>Run</button>"
                 "</form>")
    elif r["state"] == "running":
        head += "<p class=muted>Running… this page refreshes until it is done.</p>"
    return head + f"<div class=ans>{render_lines(r['lines'], store)}</div></div>"


def _color(free) -> tuple[str, str]:
    if free is None:
        return "#9aa3af", "6 5"
    if free >= 5:
        return "#2e9e5b", ""
    if free >= 1:
        return "#d99a1e", ""
    return "#d0473c", ""


def render_map(snap: dict | None, targets: dict | None, churn: dict | None, csrf: str) -> str:
    sync_form = (f"<form method=post action=/map/sync class=inline><input type=hidden "
                 f"name=csrf value='{csrf}'><button>Sync routes now</button></form>")
    if not snap:
        return f"<p>No route snapshot yet.</p>{sync_form}"
    pts: dict[str, tuple[float, float]] = {}
    unplaced: set[str] = set()

    def place(city):
        c = coords(city)
        if c:
            pts[city.lower()] = c
        elif city:
            unplaced.add(city)
        return c

    for lane in snap["lanes"]:
        place(lane["origin"])
        place(lane["destination"])
    risk = {c["client_key"]: c["risk"] for c in (churn or {}).get("clients", [])}
    for c in snap["clients"]:
        place(c.get("city"))
    kept = [t for t in (targets or {}).get("targets", []) if t["status"] == "kept"]
    for t in kept:
        place(t.get("city"))
    if not pts:
        return f"<p>No city on the lanes is in the coordinate table.</p>{sync_form}"
    lats = [p[0] for p in pts.values()]
    lons = [p[1] for p in pts.values()]
    la0, la1 = min(lats) - 0.6, max(lats) + 0.6
    lo0, lo1 = min(lons) - 0.8, max(lons) + 0.8
    k = math.cos(math.radians((la0 + la1) / 2))
    w, max_h = 760, 480
    if w * (la1 - la0) / ((lo1 - lo0) * k) > max_h:   # tall cluster: widen, keep the scale
        half = (la1 - la0) * w / max_h / k / 2
        mid = (lo0 + lo1) / 2
        lo0, lo1 = mid - half, mid + half
    h = max(260, int(w * (la1 - la0) / ((lo1 - lo0) * k)))

    def xy(c):
        return (20 + (c[1] - lo0) / (lo1 - lo0) * (w - 40),
                20 + (la1 - c[0]) / (la1 - la0) * (h - 40))

    svg = []
    for lane in snap["lanes"]:
        a, b = coords(lane["origin"]), coords(lane["destination"])
        if not (a and b):
            continue
        (x1, y1), (x2, y2) = xy(a), xy(b)
        col, dash = _color(lane["free"] if lane["known"] else None)
        label = (f"{lane['free']} free" if lane["known"] else "capacity unknown")
        svg.append(f"<line x1={x1:.0f} y1={y1:.0f} x2={x2:.0f} y2={y2:.0f} stroke='{col}' "
                   f"stroke-width=6 stroke-linecap=round stroke-dasharray='{dash}'>"
                   f"<title>{escape(lane['origin'].title())} to "
                   f"{escape(lane['destination'].title())}: {escape(lane['reason'])}</title></line>"
                   f"<text x={(x1 + x2) / 2:.0f} y={(y1 + y2) / 2 - 8:.0f} font-size=12 "
                   f"text-anchor=middle fill='currentColor'>{escape(label)}</text>")
    for city, c in pts.items():
        x, y = xy(c)
        svg.append(f"<circle cx={x:.0f} cy={y:.0f} r=4 fill='currentColor'/>"
                   f"<text x={x + 7:.0f} y={y + 4:.0f} font-size=13 "
                   f"fill='currentColor'>{escape(city.title())}</text>")
    rcol = {"high": "#d0473c", "medium": "#d99a1e", "low": "#2e9e5b"}
    for i, c in enumerate(snap["clients"]):
        cc = coords(c.get("city"))
        if not cc:
            continue
        x, y = xy(cc)
        col = rcol.get(risk.get(c["key"]), "#5b7bd5")
        svg.append(f"<rect x={x - 6 + i % 3 * 5:.0f} y={y + 8:.0f} width=10 height=10 "
                   f"fill='{col}'><title>client {escape(c['name'])}"
                   f"{' · churn risk ' + escape(risk[c['key']]) if risk.get(c['key']) else ''}"
                   f"</title></rect>")
    for i, t in enumerate(kept):
        tc = coords(t.get("city"))
        if not tc:
            continue
        x, y = xy(tc)
        svg.append(f"<path d='M{x + 4 + i % 4 * 6:.0f},{y - 22:.0f} l6,12 l-12,0 z' "
                   f"fill='#8a4fd0'><title>target {escape(t['company'])}: "
                   f"{escape((t['route_fit'] or {}).get('reason', ''))}</title></path>")
    notes = "".join(f"<p class=muted>{escape(n)}</p>" for n in snap.get("notes", []))
    miss = (f"<p class=muted>Not placed (add to routes/cities.py): "
            f"{escape(', '.join(sorted(unplaced)))}</p>" if unplaced else "")
    return f"""
<p class=muted>Routes from <b>{escape(snap['adapter'])}</b>, last sync
<b>{escape(snap['synced_at'])}</b>, capacity for the week of {escape(snap['week_of'])}.
City positions are approximate. {sync_form}</p>{notes}
<div class=legend><span><i class=sw style="background:#2e9e5b"></i>5+ free trucks</span>
<span><i class=sw style="background:#d99a1e"></i>1–4 free</span>
<span><i class=sw style="background:#d0473c"></i>full</span>
<span><i class=sw style="background:#9aa3af"></i>capacity unknown</span>
<span>▲ target (last search)</span><span>■ client (colour = churn risk)</span></div>
<svg class=map viewBox="0 0 {w} {h}" role=img aria-label="Lanes by free capacity">
{''.join(svg)}</svg>{miss}"""
