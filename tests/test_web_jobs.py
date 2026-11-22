"""Browser flow: prompt -> confirm -> job runs (same `svc run` process) -> result with
every company and its reason -> approve on the job page -> Send approved -> one email."""
import re

import pytest
from fastapi.testclient import TestClient

from service.ledger import Ledger
from service.steps.s01_intake import parse_request
from service.web import create_app
from service.web.jobs import JobRunner

PW = "pw"
PROMPT = "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"


@pytest.fixture
def web(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    runner = JobRunner(tmp_path)
    client = TestClient(create_app(led, password=PW, session_secret="s", runner=runner))
    r = client.post("/login", data={"name": "Asha", "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    yield led, runner, client, tmp_path
    led.close()


def _csrf(html: str) -> str:
    return html.split('name=csrf value="')[1].split('"')[0]


def test_home_has_prompt_box(web):
    _, _, client, _ = web
    html = client.get("/").text
    assert 'name=prompt' in html and "/jobs" in html and "/approvals" in html
    assert "data-example" in html                                 # example chips


def test_parse_shows_what_was_understood(web):
    _, _, client, _ = web
    html = client.post("/jobs/parse", data={"csrf": _csrf(client.get("/").text),
                                            "prompt": PROMPT}).text
    assert 'name=from_city value="Pune"' in html and 'name=to_city value="Mumbai"' in html
    assert 'value="5"' in html and 'value="12"' in html
    odd = client.post("/jobs/parse", data={"csrf": _csrf(client.get("/").text),
                                           "prompt": "get me some meetings"}).text
    assert "could not find two cities" in odd


def test_full_browser_flow_sends_once(web):
    led, runner, client, tmp = web
    form = client.post("/jobs/parse", data={"csrf": _csrf(client.get("/").text),
                                            "prompt": PROMPT}).text
    r = client.post("/jobs", follow_redirects=False, data={
        "csrf": _csrf(form), "prompt": PROMPT, "from_city": "Pune", "to_city": "Mumbai",
        "meetings": 5, "trucks": "12", "request_date": "2026-11-20", "budget_inr": 1500,
        "rehearsal": "1"})
    assert r.status_code == 303
    job_id = r.headers["location"].rsplit("/", 1)[1]
    assert runner.wait(job_id, timeout=120) == 0, runner.log_tail(job_id)

    page = client.get(f"/jobs/{job_id}").text
    assert "Draft ready" in page and "Kaveri Agro Foods" in page
    assert "class=tracker" in page and "Find hiring companies" in page
    assert "lookalike" in page and "do-not-contact" in page          # reasons shown
    assert "Waiting for your approval" in page

    key = next(a["key"] for a in led.pending_approvals() if a["run_id"] == job_id)
    r = client.post(f"/actions/{key}/approve", follow_redirects=False,
                    data={"csrf": _csrf(page), "next": f"/jobs/{job_id}"})
    assert r.headers["location"].startswith(f"/jobs/{job_id}?msg=")
    page = client.get(f"/jobs/{job_id}").text
    assert "Send approved (1)" in page

    r = client.post(f"/jobs/{job_id}/send", data={"csrf": _csrf(page)}, follow_redirects=False)
    assert r.status_code == 303
    assert runner.wait(job_id, timeout=120) == 0, runner.log_tail(job_id)
    assert led.get_action(key)["status"] == "done"
    assert len(list((tmp / "outbox").glob("*.eml"))) == 1
    assert len(list((tmp / "outbox").glob("*.ics"))) == 1
    assert "Sent" in client.get(f"/jobs/{job_id}").text

    # pressing Send again sends nothing new
    client.post(f"/jobs/{job_id}/send", data={"csrf": _csrf(page)})
    runner.wait(job_id, timeout=120)
    assert len(list((tmp / "outbox").glob("*.eml"))) == 1


def test_one_job_at_a_time_and_unknown_job(web):
    _, runner, client, _ = web
    assert client.get("/jobs/nope").status_code == 404
    runner.procs["busy"] = type("P", (), {"poll": lambda self: None})()
    with pytest.raises(RuntimeError, match="still running"):
        runner.start("other", {"text": "x"})


@pytest.mark.parametrize("text,route,meetings,trucks", [
    (PROMPT, ["pune", "mumbai"], 5, 12),
    ("3 meetings between Nashik and Navi Mumbai, 8 spare trucks weekly",
     ["nashik", "navi mumbai"], 3, 8),
    ("Find buyers on the Pune-Mumbai lane, we have 10 free trucks", ["pune", "mumbai"], 5, 10),
    ("Book two meetings from Chakan to Bhiwandi", ["chakan", "bhiwandi"], 2, None),
    ("Get me meetings please", [], 5, None),
])
def test_free_text_parsing(text, route, meetings, trucks):
    j = parse_request({"text": text, "request_date": "2026-11-22"})
    assert (j.route_filter, j.meetings_wanted, j.spare_trucks_per_week) == (route, meetings, trucks)
    assert re.fullmatch(r"[0-9a-f]{32}", j.job_id)
