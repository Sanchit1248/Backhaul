"""LaneLead layers: evidence, memory, task runner, route adapters, signals, chat, pages."""
import csv
import json
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import mailer
from service.budget import Budget
from service.chat import service as chat
from service.chat.answer import check, fact, note
from service.chat.parser import parse_rules
from service.config import ROOT, settings
from service.core.evidence import EvidenceStore
from service.core.memory import AccountMemory
from service.core.tasks import get_task, run_task, task, task_id
from service.ledger import Ledger
from service.models import Fact, Location, Route, Source
from service.routes.adapter import Lane
from service.routes.factory import get_adapter
from service.routes.files import FileAdapter
from service.routes.fit import route_fit
from service.routes.fleetbase import FleetbaseAdapter
from service.sources.base import SourceUnavailable
from service.web import create_app

SAMPLE = ROOT / "tests" / "fixtures" / "routes_sample.json"
DAY = date(2026, 11, 20)        # week 2026-W47 in the sample
PW = "pw-test"


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    settings()["sources"]["mode"] = "fixtures"
    monkeypatch.setitem(settings()["routes"], "adapter", "file")
    monkeypatch.setitem(settings()["routes"], "file", str(SAMPLE))
    led = Ledger(tmp_path / "t.sqlite")
    yield led
    led.close()


def src(excerpt="x = 1", d=DAY):
    return Source(url="https://example.com/r", kind="api_response", excerpt=excerpt,
                  record_date=d)


# ---- core -------------------------------------------------------------------

def test_evidence_store_roundtrip_and_stable_id(led):
    s = EvidenceStore(led)
    f = Fact(claim="Acme posted a job", value="x", sources=[src()])
    a, b = s.save_fact(f), s.save_fact(f)
    assert a == b and a.startswith("ev_")
    assert s.get_fact(a).claim == "Acme posted a job"
    assert s.get_fact("ev_missing") is None


def test_account_memory_holds_sent_not_rejected_by_default(led, monkeypatch):
    m = AccountMemory(led)
    m.record_action("c1", "Acme", "sent", job_id="j1")
    m.record_action("c1", "Acme", "sent", job_id="j1")              # idempotent per job
    assert len(m.history("c1")) == 1
    assert "contacted on" in m.hold_reason("c1", exclude_job="j2")
    assert m.hold_reason("c1", exclude_job="j1") is None             # its own job
    m.record_action("c2", "Beta", "rejected", job_id="j1")
    assert m.hold_reason("c2", exclude_job="j2") is None
    monkeypatch.setitem(settings()["memory"], "rejected_hold_days", 30)
    assert "rejected" in m.hold_reason("c2", exclude_job="j2")
    assert m.forget("c1") == 1 and m.history("c1") == []


def test_task_runner_is_deterministic_and_records_failures(led):
    calls = []

    @task("t_echo")
    def echo(ledger, params):
        calls.append(params["x"])
        return {"x": params["x"]}

    @task("t_boom")
    def boom(ledger, params):
        raise RuntimeError("nope")

    a = run_task(led, "t_echo", {"x": 1})
    b = run_task(led, "t_echo", {"x": 1})
    assert a["task_id"] == b["task_id"] == task_id("t_echo", {"x": 1})
    assert calls == [1] and b["result"] == {"x": 1}                  # done -> not re-run
    with pytest.raises(RuntimeError):
        run_task(led, "t_boom", {})
    assert get_task(led, task_id("t_boom", {}))["status"] == "failed"


# ---- routes: the file adapter (JSON + CSV) and Fleetbase pass the same checks ------

def _csv_dir(tmp_path: Path) -> Path:
    data = json.loads(SAMPLE.read_text())
    d = tmp_path / "csv"
    d.mkdir()
    for name in ("vehicles", "clients", "orders"):
        rows = data[name]
        with (d / f"{name}.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return d


def _fleetbase_json() -> dict:
    """The sample, shaped like Fleetbase v1 API responses."""
    data = json.loads(SAMPLE.read_text())
    cities = {c["id"]: c for c in data["clients"]}
    return {
        "vehicles": [{"id": v["id"], "name": v["id"], "payload_capacity": v["payload_capacity_kg"],
                      "meta": {"lane": v["lane"], "trips_per_week": v["trips_per_week"]}}
                     for v in data["vehicles"]],
        "contacts": {"data": [{"id": c["id"], "name": c["name"], "type": "customer",
                               "meta": {"city": c["city"]}} for c in data["clients"]]},
        "orders": [{"id": o["id"], "status": o["status"], "scheduled_at": o["scheduled_at"],
                    "customer": {"id": o["customer"], "name": cities[o["customer"]]["name"]},
                    "payload": {"pickup": {"city": o["origin"].title()},
                                "dropoff": {"city": o["destination"].title()}}}
                   for o in data["orders"]],
    }


@pytest.fixture(params=["json", "csv", "fleetbase"])
def adapter(request, led, tmp_path, monkeypatch):
    if request.param == "json":
        return FileAdapter(SAMPLE)
    if request.param == "csv":
        return FileAdapter(_csv_dir(tmp_path))
    fb = _fleetbase_json()

    def fake_fetch(ledger, budget, *, op, url, headers, params, **kw):
        assert headers["Authorization"] == "Bearer flb_test"
        return {"data": fb[op], "url": url, "retrieved_at": "2026-11-20T10:00:00+00:00"}

    monkeypatch.setattr("service.routes.fleetbase.fetch_json", fake_fetch)
    return FleetbaseAdapter(led, Budget(led, "t"), base_url="http://fb.test", api_key="flb_test")


def test_adapter_lanes_capacity_clients_volume(adapter):
    keys = {lane.key for lane in adapter.list_routes()}
    assert keys == {"mumbai-pune", "mumbai-nashik", "nashik-pune"}
    cap = adapter.capacity(Lane("pune", "mumbai"), DAY)
    assert (cap.known, cap.trucks_per_week, cap.booked, cap.free, cap.week) == \
        (True, 16, 4, 12, "2026-W47")
    assert cap.sources and all(s.excerpt for s in cap.sources)
    unknown = adapter.capacity(Lane("pune", "nashik"), DAY)
    assert not unknown.known and unknown.free is None and "capacity unknown" in unknown.reason
    names = {c.name for c in adapter.list_clients()}
    assert "Sahyog Polymers Pvt Ltd" in names
    vol = adapter.client_volume("CL-02", 16, DAY)
    assert [w["orders"] for w in vol][:2] == [6, 6] and vol[-1]["orders"] == 1


def test_fleetbase_without_key_falls_back_to_file_with_note(led, monkeypatch):
    monkeypatch.setitem(settings()["routes"], "adapter", "fleetbase")
    monkeypatch.delenv("FLEETBASE_API_KEY", raising=False)
    with pytest.raises(SourceUnavailable):
        FleetbaseAdapter(led, Budget(led, "t"), api_key="").data()
    notes = []
    a = get_adapter(led, Budget(led, "t"), notes)
    assert isinstance(a, FileAdapter) and "Fleetbase unavailable" in notes[0]


def test_route_fit_function():
    loc = Location(city="Chakan", kind="plant", source=src("locations[0] = Chakan plant"))
    lanes = [Route(origin="pune", destination="mumbai", spare_trucks_per_week=12)]
    r = route_fit("Acme", [loc], lanes)
    assert r["level"] == "partial" and "near Pune" in r["reason"] and r["matched"]
    assert route_fit("Acme", [], lanes)["level"] == "none"


# ---- signals ----------------------------------------------------------------

def test_find_targets_kept_have_resolvable_evidence(led):
    t = run_task(led, "find_targets", {"request_date": "2026-11-20", "count": 5})
    r = t["result"]
    by_lane = {lane["lane"]: lane for lane in r["lanes"]}
    assert "capacity unknown" in by_lane["nashik-pune"]["skipped"]
    assert by_lane["mumbai-pune"]["capacity"]["free"] == 12
    kept = [x for x in r["targets"] if x["status"] == "kept"]
    assert {x["company"] for x in kept} == {"Kaveri Agro Foods Pvt Ltd", "Deccan Pharma Ltd"}
    assert len({x["company_key"] for x in r["targets"]}) == len(r["targets"])   # one row each
    store = EvidenceStore(led)
    for x in kept:
        assert x["evidence_ids"] and all(store.get_fact(i) for i in x["evidence_ids"])
        assert x["outbox_key"] and led.get_action(x["outbox_key"])["status"] == "pending_approval"
    assert {x["status"] for x in r["targets"]} <= {"kept", "held", "cannot_verify"}
    # same question again -> same task, nothing re-run
    assert run_task(led, "find_targets", {"request_date": "2026-11-20", "count": 5})["task_id"] \
        == t["task_id"]


def test_sent_company_is_held_in_a_later_search(led):
    first = run_task(led, "find_targets", {"request_date": "2026-11-20",
                                           "routes": [["pune", "mumbai"]]})["result"]
    key = next(x["company_key"] for x in first["targets"]
               if x["company"] == "Kaveri Agro Foods Pvt Ltd")
    for a in led.pending_approvals():        # rejected drafts do not block (recontact rule)
        led.decide(a["key"], False, by="t")
    AccountMemory(led).record_action(key, "Kaveri Agro Foods Pvt Ltd", "sent", job_id="older")
    r = run_task(led, "find_targets", {"request_date": "2026-11-20", "window_days": 31,
                                       "routes": [["pune", "mumbai"]]})["result"]
    kav = next(x for x in r["targets"] if x["company"] == "Kaveri Agro Foods Pvt Ltd")
    assert kav["status"] == "held" and "contacted on" in kav["reason"]


def test_check_churn_levels(led):
    r = run_task(led, "check_churn", {"until": "2026-11-20"})["result"]
    by = {c["client"]: c for c in r["clients"]}
    assert by["Sahyog Polymers Pvt Ltd"]["risk"] == "high"
    assert by["Mahalaxmi Textiles"]["risk"] == "low"
    assert by["Western Ghats Agro"]["status"] == "cannot_verify"
    store = EvidenceStore(led)
    assert all(store.get_fact(i) for i in by["Sahyog Polymers Pvt Ltd"]["evidence_ids"])


# ---- chat -------------------------------------------------------------------

CASES = json.loads((ROOT / "tests" / "fixtures" / "chat_questions.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["q"][:40] for c in CASES])
def test_chat_parser_questions(case):
    t = parse_rules(case["q"])
    assert t.type == case["task"]
    for k, v in case["params"].items():
        assert t.params.get(k) == v, (k, t.params)
    if case["task"] == "find_targets":
        assert t.needs_confirm                    # paid task waits for a click


def test_answer_check_drops_unsourced_fact_lines(led):
    store = EvidenceStore(led)
    eid = store.save_fact(Fact(claim="c", sources=[src()]))
    out = check([fact("ok", [eid]), fact("made up", []), fact("bad id", ["ev_nope"]),
                 note("held: x")], store)
    assert out[0]["kind"] == "fact"
    assert out[1]["text"].startswith("cannot verify") and out[2]["text"].startswith("cannot")
    assert out[3]["kind"] == "note"


def _wait(led, tid, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = get_task(led, tid)
        if t and t["status"] != "running":
            return t
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_chat_follow_up_second_one(led):
    conv = "c_test"
    t1 = chat.ask(led, conv, "Asha", "Find 5 targets on Pune to Mumbai")
    assert chat.render(led, chat.ConversationMemory(led).turn(t1))["state"] == "parsed"
    tid = chat.run(led, t1)
    _wait(led, tid)
    first = chat.render(led, chat.ConversationMemory(led).turn(t1))
    assert first["state"] == "done"
    assert all(ln["evidence_ids"] for ln in first["lines"] if ln["kind"] == "fact")
    t2 = chat.ask(led, conv, "Asha", "draft an email to the second one")
    r = chat.render(led, chat.ConversationMemory(led).turn(t2))
    pre = [ln for ln in r["lines"] if ln["kind"] == "pre"]
    assert pre and "unsubscribe" in pre[0]["text"]
    assert any(ln["kind"] == "link" for ln in r["lines"])
    t3 = chat.ask(led, conv, "Asha", "draft an email to the ninth one")
    assert "pick 1 to" in chat.render(led, chat.ConversationMemory(led).turn(t3))["lines"][0][
        "text"]


# ---- pages + request API ------------------------------------------------------

@pytest.fixture
def client(led, monkeypatch):
    c = TestClient(create_app(led, password=PW, session_secret="s"))
    c.post("/login", data={"name": "Asha", "password": PW})
    return c


def _csrf(c, path="/chat"):
    return c.get(path).text.split('name=csrf value="')[1].split('"')[0]


def test_pages_render_and_memory_delete(client, led):
    tok = _csrf(client)
    client.post("/chat/ask", data={"csrf": tok, "conv": "c_web",
                                   "question": "What clients might be leaving?"})
    tid = chat.ConversationMemory(led).turns("c_web")[0]["task_id"]
    _wait(led, tid)
    html = client.get("/chat?c=c_web").text
    assert "Sahyog Polymers Pvt Ltd: high risk" in html and "sources (" in html
    m = client.get("/map")
    assert "Sync routes now" in m.text
    client.post("/map/sync", data={"csrf": tok})
    m = client.get("/map").text
    assert "<svg" in m and "12 free" in m and "capacity unknown" in m
    assert "c_web" in client.get("/memory").text
    client.post("/memory/conversation", data={"csrf": tok, "conv": "c_web"})
    assert chat.ConversationMemory(led).turns("c_web") == []


def test_request_api_auth_and_result(led):
    c = TestClient(create_app(led, password=PW, session_secret="s"))
    req = {"job_id": "api-job", "request_date": "2026-11-20",
           "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"}
    assert c.post("/api/request", json=req).status_code == 401
    c.post("/login", data={"name": "Asha", "password": PW})
    r = c.post("/api/request", json=req)
    assert r.status_code == 200
    body = r.json()
    for k in settings()["output"]["required_fields"]:
        assert k in body
    assert body["status"] == "awaiting_approval"


def test_approve_from_page_records_account_memory(client, led):
    run_task(led, "find_targets", {"request_date": "2026-11-20", "routes": [["pune", "mumbai"]]})
    a = led.pending_approvals()[0]
    tok = client.get("/").text.split('name=csrf value="')[1].split('"')[0]
    client.post(f"/actions/{a['key']}/approve", data={"csrf": tok})
    hist = AccountMemory(led).history()
    assert any(h["action"] == "approved" and h["outcome"] == "by Asha" for h in hist)
