"""Guards the two properties judges test live: kill+restart resumes, and each
action happens exactly once."""
import pytest

from service.ledger import Ledger, idem_key
from service.models import Fact, Source


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "t.sqlite")


def test_step_checkpoint_runs_once(ledger):
    calls = []
    f = lambda: calls.append(1) or {"x": 1}
    assert ledger.step("r1", "gather", f) == {"x": 1}
    assert ledger.step("r1", "gather", f) == {"x": 1}
    assert len(calls) == 1


def test_action_happens_once_across_restart(tmp_path):
    db = tmp_path / "t.sqlite"
    effects = []
    effect = lambda p: effects.append(p["idempotency_key"]) or {"ok": True}
    key = idem_key("payment", "INV-1", "Swiggy Limited", 1240)

    Ledger(db).action("r1", "payment", key, {"amt": 1240}, effect, needs_approval=False)
    # simulate process restart: new Ledger object on same file
    res = Ledger(db).action("r1", "payment", key, {"amt": 1240}, effect, needs_approval=False)
    assert res["deduped"] is True
    assert effects == [key]


def test_irreversible_action_waits_for_approval(ledger):
    effects = []
    key = idem_key("payment", "INV-2")
    effect = lambda p: effects.append(1) or {"ok": True}
    r = ledger.action("r1", "payment", key, {}, effect, needs_approval=True)
    assert r["status"] == "pending_approval" and not effects
    ledger.decide(key, approve=True, by="tester")
    r = ledger.action("r1", "payment", key, {}, effect, needs_approval=True)
    assert r["status"] == "done" and effects == [1]


def test_rejected_action_never_runs(ledger):
    key = idem_key("send_email", "x")
    ledger.action("r1", "send_email", key, {}, lambda p: 1 / 0, needs_approval=True)
    ledger.decide(key, approve=False, by="tester")
    assert ledger.action("r1", "send_email", key, {}, lambda p: 1 / 0,
                         needs_approval=True)["status"] == "rejected"


def test_cost_ledger_sums(ledger):
    ledger.cost("r1", "anthropic", "llm", 0.01)
    ledger.cost("r1", "crustdata", "company", 0.002)
    assert ledger.run_cost("r1") == pytest.approx(0.012)


def test_fact_requires_source():
    with pytest.raises(ValueError):
        Fact(claim="Payee is X", value="X", sources=[])
    with pytest.raises(ValueError):
        Fact(claim="Payee is X", value="X",
             sources=[Source(url="https://x", kind="web_page", excerpt="  ")])
