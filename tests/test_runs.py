"""Claims, leases, fencing and failure recovery (sections 07, 14)."""

from __future__ import annotations

import threading

from relay import config


def test_claim_guards(relay, clock):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()["task_id"]
    a_dev, _ = relay.pair(a)
    assert relay.claim(a, t, a_dev).json()["error"]["code"] == "FORBIDDEN"  # own task

    other_caps = {"adapters": {"claude_code": {"models": ["*"], "usage": "known"}}, "tools": []}
    mismatch_id, _ = relay.pair(b, caps=other_caps)
    assert relay.claim(b, t, mismatch_id).json()["error"]["code"] == "CAPABILITY_MISMATCH"

    no_usage = {"adapters": {"echo": {"models": ["*"], "usage": "unknown"}}, "tools": []}
    nu_id, _ = relay.pair(b, caps=no_usage)
    assert relay.claim(b, t, nu_id).json()["error"]["code"] == "CAPABILITY_MISMATCH"

    idle_id, _ = relay.pair(b, ready=False)
    assert relay.claim(b, t, idle_id).json()["error"]["code"] == "DEVICE_NOT_READY"

    dev_id, _ = relay.pair(b)
    clock.advance(config.DEVICE_READY_WINDOW_S + 1)
    assert relay.claim(b, t, dev_id).json()["error"]["code"] == "DEVICE_NOT_READY"


def test_concurrent_claims_have_one_winner(relay):
    a = relay.login("alice")
    t = relay.create(a).json()["task_id"]
    claimers = []
    for i in range(6):
        u = relay.login(f"worker{i}")
        dev_id, _ = relay.pair(u)
        claimers.append((u, dev_id))
    results = []
    barrier = threading.Barrier(len(claimers))

    def go(u, d):
        barrier.wait()
        results.append(relay.claim(u, t, d))

    threads = [threading.Thread(target=go, args=c) for c in claimers]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1
    assert all(r.json()["error"]["code"] == "TASK_ALREADY_CLAIMED" for r in results if r.status_code != 200)


def test_device_holds_one_run_at_a_time(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t1 = relay.create(a, budget_rp=5).json()["task_id"]
    t2 = relay.create(a, budget_rp=5).json()["task_id"]
    dev_id, _ = relay.pair(b)
    assert relay.claim(b, t1, dev_id).status_code == 200
    assert relay.claim(b, t2, dev_id).json()["error"]["code"] == "INVALID_STATE"


def test_start_is_required_and_fenced(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()["task_id"]
    dev_id, dev = relay.pair(b)
    relay.claim(b, t, dev_id)
    run = relay.next(dev)
    hb = relay.c.post(f"/v2/runs/{run['run_id']}/heartbeat", headers=dev, json={"fencing_token": run["fencing_token"]})
    assert hb.status_code == 409  # heartbeat before start is not allowed
    assert relay.start(dev, dict(run, fencing_token=run["fencing_token"] + 1)).status_code == 409
    assert relay.start(dev, run).status_code == 200
    assert relay.start(dev, run).status_code == 200  # idempotent retry

    _, other_dev = relay.pair(relay.login("carol"))
    assert relay.start(other_dev, run).json()["error"]["code"] == "FORBIDDEN"


def test_lease_expiry_stalls_task_and_late_receipt_is_evidence_only(relay, clock):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()["task_id"]
    dev_id, dev = relay.pair(b)
    relay.claim(b, t, dev_id)
    run = relay.next(dev)
    relay.start(dev, run)
    clock.advance(config.RUN_LEASE_S + 1)

    task = relay.c.get(f"/v2/tasks/{t}", headers=a).json()
    assert task["status"] == "stalled" and task["runs"][0]["state"] == "expired"
    late = relay.finish(dev, run)
    assert late.status_code == 409 and late.json()["error"]["code"] == "LEASE_EXPIRED"
    assert relay.me(b)["account"]["balance_milli"] == 0
    assert relay.me(a)["account"]["reserved_milli"] == 10_000  # still reserved while stalled

    # Reopen needs explicit acknowledgement of possible duplicate cost.
    assert relay.c.post(f"/v2/tasks/{t}/reopen", headers=a, json={}).status_code == 422
    assert relay.c.post(f"/v2/tasks/{t}/reopen", headers=a, json={"acknowledge_duplicate_cost": True}).status_code == 200

    relay.c.post("/v2/devices/heartbeat", headers=dev, json={"ready": True})
    assert relay.claim(b, t, dev_id).status_code == 200
    run2 = relay.next(dev)
    assert run2["fencing_token"] == run["fencing_token"] + 1
    relay.start(dev, run2)
    # Old token cannot advance the new run; new token settles once.
    assert relay.finish(dev, run2, token=run["fencing_token"]).status_code == 409
    ok = relay.finish(dev, run2)
    assert ok.status_code == 200 and ok.json()["fee_milli"] == 5000
    assert relay.reconcile()["ok"]


def test_heartbeat_extends_lease_up_to_hard_limit(relay, clock):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a, max_runtime_s=60).json()["task_id"]
    dev_id, dev = relay.pair(b)
    relay.claim(b, t, dev_id)
    run = relay.next(dev)
    relay.start(dev, run)
    body = {"fencing_token": run["fencing_token"]}
    # Assign lease (120) + runtime (60) + grace (120) = 300s hard limit from claim.
    for _ in range(4):  # up to T+240; the last one is clamped to the T+300 hard limit
        clock.advance(60)
        assert relay.c.post(f"/v2/runs/{run['run_id']}/heartbeat", headers=dev, json=body).status_code == 200
    clock.advance(61)
    assert relay.c.post(f"/v2/runs/{run['run_id']}/heartbeat", headers=dev, json=body).status_code == 409


def test_stop_request_reaches_device(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()["task_id"]
    dev_id, dev = relay.pair(b)
    relay.claim(b, t, dev_id)
    run = relay.next(dev)
    relay.start(dev, run)
    stranger = relay.login("mallory")
    assert relay.c.post(f"/v2/runs/{run['run_id']}/stop", headers=stranger).status_code == 403
    assert relay.c.post(f"/v2/runs/{run['run_id']}/stop", headers=a).status_code == 200
    hb = relay.c.post(f"/v2/runs/{run['run_id']}/heartbeat", headers=dev, json={"fencing_token": run["fencing_token"]})
    assert hb.json()["stop_requested"] is True
    # Usage already incurred still settles.
    assert relay.finish(dev, run, status="failed", output="").json()["fee_milli"] == 5000


def test_revoked_device_cannot_act(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t = relay.create(a).json()["task_id"]
    dev_id, dev = relay.pair(b)
    relay.claim(b, t, dev_id)
    run = relay.next(dev)
    assert relay.c.post(f"/v2/devices/{dev_id}/revoke", headers=b).status_code == 200
    r = relay.start(dev, run)
    assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"


def test_pair_code_single_use(relay):
    b = relay.login("bob")
    code = relay.c.post("/v2/devices/pair-codes", headers=b).json()["code"]
    assert relay.c.post("/v2/devices/pair", json={"code": code}).status_code == 200
    r = relay.c.post("/v2/devices/pair", json={"code": code})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PAIR_CODE_INVALID"


def test_task_list_is_summary_and_paginated(relay):
    a = relay.login("alice")
    for i in range(5):
        relay.create(a, budget_rp=1, title=f"t{i}")
    page1 = relay.c.get("/v2/tasks?limit=3", headers=a).json()
    assert len(page1["tasks"]) == 3 and "spec" not in page1["tasks"][0] and page1["next_cursor"]
    page2 = relay.c.get(f"/v2/tasks?limit=3&before={page1['next_cursor']}", headers=a).json()
    assert len(page2["tasks"]) == 2 and page2["next_cursor"] is None
    ids = {t["id"] for t in page1["tasks"] + page2["tasks"]}
    assert len(ids) == 5


def test_one_tap_claim_picks_compatible_idle_device(relay):
    a, b = relay.login("alice"), relay.login("bob")
    t1 = relay.create(a, budget_rp=5).json()["task_id"]
    t2 = relay.create(a, budget_rp=5).json()["task_id"]
    t3 = relay.create(a, budget_rp=5).json()["task_id"]

    r = relay.c.post(f"/v2/tasks/{t1}/claim", headers=b, json={})
    assert r.json()["error"]["code"] == "NO_DEVICE"
    other = {"adapters": {"claude_code": {"models": ["*"], "usage": "known"}}, "tools": []}
    relay.pair(b, caps=other)
    assert relay.c.post(f"/v2/tasks/{t1}/claim", headers=b, json={}).json()["error"]["code"] == "CAPABILITY_MISMATCH"

    dev_id, _ = relay.pair(b)
    r = relay.c.post(f"/v2/tasks/{t1}/claim", headers=b, json={})
    assert r.status_code == 200 and r.json()["device_id"] == dev_id
    # The only compatible device is busy now.
    assert relay.c.post(f"/v2/tasks/{t2}/claim", headers=b, json={}).json()["error"]["code"] == "INVALID_STATE"
    dev2, _ = relay.pair(b)
    assert relay.c.post(f"/v2/tasks/{t2}/claim", headers=b, json={}).json()["device_id"] == dev2
    assert relay.c.post(f"/v2/tasks/{t3}/claim", headers=b, json={}).json()["error"]["code"] == "INVALID_STATE"


def test_supply_aggregates_online_models(relay, clock):
    b = relay.login("bob")
    relay.pair(b)
    relay.pair(relay.login("carol"))
    relay.pair(b, caps={"adapters": {"codex": {"models": ["gpt-x"], "usage": "unknown"}}, "tools": []})
    s = relay.c.get("/v2/supply", headers=b).json()["options"]
    assert s == [{"adapter": "echo", "model": "echo-1", "devices": 2}]
    clock.advance(config.DEVICE_READY_WINDOW_S + 1)
    assert relay.c.get("/v2/supply", headers=b).json()["options"] == []
