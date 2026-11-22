"""Approval web app (C): login, evidence shown, approve/edit/reject -> ledger, never sends."""
from datetime import date
from email import message_from_bytes
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import mailer
from service.config import settings
from service.ledger import Ledger
from service.mailer import build_ics
from service.pipeline import run
from service.web import create_app
from service.web.app import check_edit

REQ = {"job_id": "web-job", "request_date": "2026-11-20",
       "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"}
PW = "test-approver-pw"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    settings()["sources"]["mode"] = "fixtures"
    led = Ledger(tmp_path / "t.sqlite")
    run(REQ, led)
    client = TestClient(create_app(led, password=PW, session_secret="s"))
    yield led, client, tmp_path
    led.close()


def login(client, name="Asha Rao"):
    r = client.post("/login", data={"name": name, "password": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def csrf(client):
    html = client.get("/").text
    return html.split('name=csrf value="')[1].split('"')[0]


def first_pending(led):
    return led.pending_approvals()[0]


def test_login_required_and_wrong_password(setup):
    _, client, _ = setup
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login", data={"name": "x", "password": "nope"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    a_key = "whatever"
    assert client.post(f"/actions/{a_key}/approve", data={"csrf": "x"}).status_code == 401


def test_login_disabled_without_password(tmp_path):
    led = Ledger(tmp_path / "t.sqlite")
    c = TestClient(create_app(led, password=""))
    assert "login is disabled" in c.get("/login").text
    assert c.post("/login", data={"name": "a", "password": ""}).status_code in (422, 503)
    led.close()


def test_page_shows_email_claims_sources_fit_buyer_cost(setup):
    led, client, _ = setup
    login(client)
    html = client.get("/").text
    a = first_pending(led)
    p, d = a["payload"], a["payload"]["draft"]
    assert d["to"] in html and "Approve" in html and "Reject" in html and "Edit" in html
    for c in d["claims"]:
        for s in c["sources"]:
            assert s["url"].split("?")[0][:40] in html
            assert "record date" in html
    assert p["route_fit_detail"]["reason"].split(";")[0] in html
    assert p["buyer"]["name"] in html and "employer id matches company" in html
    assert "cost so far" in html


def test_approve_records_name_and_does_not_send(setup):
    led, client, tmp = setup
    login(client)
    a = first_pending(led)
    r = client.post(f"/actions/{a['key']}/approve", data={"csrf": csrf(client)})
    assert r.status_code == 200
    got = led.get_action(a["key"])
    assert got["status"] == "approved" and got["approved_by"] == "Asha Rao"
    assert not (tmp / "outbox").exists() or not list((tmp / "outbox").glob("*.eml"))
    # second decision on the same action is refused
    assert client.post(f"/actions/{a['key']}/reject",
                       data={"csrf": csrf(client)}).status_code == 409


def test_reject(setup):
    led, client, _ = setup
    login(client, "Vikram")
    a = first_pending(led)
    client.post(f"/actions/{a['key']}/reject", data={"csrf": csrf(client)})
    got = led.get_action(a["key"])
    assert got["status"] == "rejected" and got["approved_by"] == "Vikram"


def test_bad_csrf_refused(setup):
    led, client, _ = setup
    login(client)
    a = first_pending(led)
    assert client.post(f"/actions/{a['key']}/approve",
                       data={"csrf": "forged"}).status_code == 403
    assert led.get_action(a["key"])["status"] == "pending_approval"


def test_edit_updates_payload_and_stays_pending(setup):
    led, client, _ = setup
    login(client)
    a = first_pending(led)
    d = a["payload"]["draft"]
    body = d["body"].replace("Would a 20-minute call", "Could a short call")
    r = client.post(f"/actions/{a['key']}/edit",
                    data={"csrf": csrf(client), "subject": "New subject", "body": body})
    assert r.status_code == 200
    got = led.get_action(a["key"])
    assert got["status"] == "pending_approval"
    assert got["payload"]["draft"]["subject"] == "New subject"
    assert got["payload"]["draft"]["edited_by"] == "Asha Rao"
    assert got["payload"]["draft"]["to"] == d["to"]          # recipient untouched
    assert got["payload"]["draft"]["claims"] == d["claims"]  # evidence untouched


def test_edit_rejects_unsourced_citation_and_missing_unsubscribe(setup):
    led, client, _ = setup
    login(client)
    a = first_pending(led)
    d = a["payload"]["draft"]
    r = client.post(f"/actions/{a['key']}/edit", follow_redirects=False,
                    data={"csrf": csrf(client), "subject": d["subject"],
                          "body": d["body"] + "\nAlso you raised money [9]."})
    assert "error" in r.headers["location"]
    assert led.get_action(a["key"])["payload"]["draft"]["body"] == d["body"]


def test_check_edit_rules():
    assert check_edit("s", "hi unsubscribe [1]", 1) is None
    assert "one line" in check_edit("a\nBcc: x@y", "unsubscribe", 1)
    assert "unsubscribe" in check_edit("s", "hi", 1)
    assert "[2]" in check_edit("s", "unsubscribe [2]", 1)


def test_web_app_never_imports_sender():
    import ast

    import service.web.app as w
    tree = ast.parse(Path(w.__file__).read_text(encoding="utf-8"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("mailer" in m or "s10_send" in m or "smtplib" in m for m in mods)


# ---- calendar invite (Google Calendar / Outlook compatibility) ---------------

def _unfold(ics):
    return ics.replace("\r\n ", "")


def test_ics_rfc5545_shape():
    ics = build_ics(uid="p1", title="Acme Logistics, Pvt; Ltd x Kaveri Agro Foods Pvt Ltd",
                    attendee="priya@kaveri.example", attendee_name="Priya Nair",
                    organizer="sales@acme.example", organizer_name="Sales Head, Acme",
                    description="20-minute call: spare truck capacity on Pune to Mumbai",
                    minutes=20, start_after=date(2026, 11, 20))
    assert ics.endswith("\r\n") and "\n" not in ics.replace("\r\n", "")
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    u = _unfold(ics)
    for prop in ("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:", "METHOD:REQUEST", "BEGIN:VEVENT",
                 "UID:p1@contextcon05", "DTSTAMP:20261120T000000Z",
                 "DTSTART:20261123T053000Z", "DTEND:20261123T055000Z",  # Mon 11:00 IST
                 'ORGANIZER;CN="Sales Head, Acme":mailto:sales@acme.example',
                 "END:VEVENT", "END:VCALENDAR"):
        assert prop in u, prop
    assert r"SUMMARY:Acme Logistics\, Pvt\; Ltd x Kaveri Agro Foods Pvt Ltd" in u
    assert 'ATTENDEE;CN="Priya Nair";ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE' in u


def test_ics_without_organizer_is_publish():
    ics = build_ics(uid="p1", title="t", attendee="a@b.example", minutes=20,
                    start_after=date(2026, 11, 20))
    assert "METHOD:PUBLISH" in ics and "ORGANIZER" not in ics and "RSVP=FALSE" in ics


def test_eml_calendar_part_has_method(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    ics = build_ics(uid="p1", title="t", attendee="a@b.example", organizer="s@c.example",
                    minutes=20, start_after=date(2026, 11, 20))
    res = mailer.send({"idempotency_key": "k1", "draft": {
        "to": "a@b.example", "subject": "s", "body": "b", "ics": ics}})
    msg = message_from_bytes((tmp_path / "outbox" / "k1.eml").read_bytes())
    cal = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert cal.get_param("method") == "REQUEST"
    assert cal.get_payload(decode=True).decode() == ics
    assert (tmp_path / "outbox" / "k1.ics").read_bytes() == ics.encode()
    assert res["message_id"] == "k1"
