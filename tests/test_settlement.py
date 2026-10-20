"""Ledger rules and settlement idempotency (sections 08, 12, 14)."""

from __future__ import annotations

from relay import service


def _claimed_run(relay, budget_rp=10, **over):
    a, b = relay.login("alice"), relay.login("bob")
    task = relay.create(a, budget_rp=budget_rp, **over).json()
    dev_id, dev = relay.pair(b)
    assert relay.claim(b, task["task_id"], dev_id).status_code == 200
    run = relay.next(dev)
    assert relay.start(dev, run).status_code == 200
    return a, b, dev, run


def test_fee_formula_caps_at_budget():
    assert service.compute_fee_milli(10_000, 2000, 1000) == 5000
    assert service.compute_fee_milli(1_000, 2000, 1000) == 1000
    assert service.compute_fee_milli(10_000, 0, 0) == 0


def test_design_doc_worked_example(relay):
    """A: balance 0, credit 20 RP, publishes 10 RP; 2,000 in + 1,000 out -> 5 RP."""
    a, b, dev, run = _claimed_run(relay)
    assert relay.me(a)["account"]["reserved_milli"] == 10_000
    r = relay.finish(dev, run)
    assert r.status_code == 200, r.text
    assert r.json()["fee_milli"] == 5000 and r.json()["outcome"] == "succeeded"
    acct_a, acct_b = relay.me(a)["account"], relay.me(b)["account"]
    assert (acct_a["balance_milli"], acct_a["reserved_milli"]) == (-5000, 0)
    assert acct_b["balance_milli"] == 5000
    # Negative balance blocks publishing but not claiming.
    blocked = relay.create(a, budget_rp=1)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "INSUFFICIENT_CREDIT"
    assert relay.reconcile() == {"ok": True, "problems": {}}


def test_duplicate_receipt_is_idempotent_and_conflicting_receipt_rejected(relay):
    a, b, dev, run = _claimed_run(relay)
    first = relay.finish(dev, run).json()
    again = relay.finish(dev, run)
    assert again.status_code == 200 and again.json() == first
    assert relay.me(b)["account"]["balance_milli"] == 5000
    other = relay.finish(dev, run, output="different")
    assert other.status_code == 409 and other.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert relay.me(b)["account"]["balance_milli"] == 5000
    assert relay.reconcile()["ok"]


def test_unknown_usage_settles_as_zero(relay):
    a, b, dev, run = _claimed_run(relay)
    r = relay.finish(dev, run, status="failed", output="", usage={"status": "unknown"}).json()
    assert (r["fee_milli"], r["outcome"], r["usage_status"]) == (0, "failed", "unknown")
    assert relay.me(a)["account"]["reserved_milli"] == 0
    assert relay.me(a)["account"]["balance_milli"] == 0
    assert relay.reconcile()["ok"]


def test_failed_validation_with_known_usage_still_pays(relay):
    a, b, dev, run = _claimed_run(relay, validator={"id": "json", "params": {}})
    r = relay.finish(dev, run, output="not json").json()
    assert r["outcome"] == "failed" and r["fee_milli"] == 5000
    assert r["validation"]["passed"] is False
    task = relay.c.get(f"/v2/tasks/{run['task_id']}", headers=a).json()
    assert task["status"] == "failed"
    assert relay.reconcile()["ok"]


def test_publish_limits(relay):
    a = relay.login("alice")
    assert relay.create(a, budget_rp=0).status_code == 422
    assert relay.create(a, budget_rp=101).status_code == 422
    assert relay.create(a, budget_rp=15).status_code == 200
    r = relay.create(a, budget_rp=10)  # 0 - 15 + 20 = 5 RP available
    assert r.json()["error"]["code"] == "INSUFFICIENT_CREDIT"
    assert relay.create(a, budget_rp=5).status_code == 200
    assert relay.create(a, public_consent=False).status_code == 422


def test_cancel_releases_reservation(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()
    assert relay.c.post(f"/v2/tasks/{t['task_id']}/cancel", headers=b).status_code == 403
    assert relay.c.post(f"/v2/tasks/{t['task_id']}/cancel", headers=a).status_code == 200
    assert relay.me(a)["account"]["reserved_milli"] == 0
    assert relay.c.post(f"/v2/tasks/{t['task_id']}/cancel", headers=a).status_code == 409
    assert relay.reconcile()["ok"]


def test_create_idempotency_key(relay):
    a = relay.login("alice")
    r1 = relay.create(a, key="k1").json()
    r2 = relay.create(a, key="k1").json()
    assert r1 == r2
    assert relay.me(a)["account"]["reserved_milli"] == 10_000
    r3 = relay.create(a, key="k1", title="changed")
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_spec_hash_mismatch_rejected(relay):
    a, b, dev, run = _claimed_run(relay)
    bad = dict(run, spec_hash="0" * 64)
    r = relay.finish(dev, bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ARTIFACT_INVALID"


def test_echo_adapter_blocked_unless_enabled(tmp_path, clock):
    from fastapi.testclient import TestClient
    from relay.api import create_app
    from relay.config import Settings
    from tests.conftest import Relay

    r = Relay(TestClient(create_app(Settings(db_path=str(tmp_path / "x.db"), allow_dev_login=True, allow_test_adapters=False,
                                                     rate_limits=False))))
    resp = r.create(r.login("alice"))
    assert resp.status_code == 422
