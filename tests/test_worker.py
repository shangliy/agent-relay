"""Local worker end-to-end against the API, plus adapter parsers."""

from __future__ import annotations

import json
import threading

import pytest

from agent_relay_worker import worker as w
from agent_relay_worker.adapters import ClaudeCodeAdapter, CodexAdapter, EchoAdapter, HttpAdapter
from relay import spec as server_spec
from relay.templates import TEMPLATES


def make_transport(client, fail_paths: set[str] | None = None):
    fail_paths = fail_paths if fail_paths is not None else set()

    def transport(method, url, headers, body):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        if any(path.endswith(p) for p in fail_paths):
            fail_paths.clear()  # fail once, like a dropped response
            raise w.ApiError(0, "NETWORK", "simulated drop")
        r = client.request(method, path, headers=headers, json=body)
        return r.status_code, r.json()

    return transport


@pytest.fixture
def paired_worker(relay, client, tmp_path):
    bob = relay.login("bob")
    code = client.post("/v2/devices/pair-codes", headers=bob).json()["code"]
    state = w.State(tmp_path / "state")
    adapters = {"echo": EchoAdapter()}
    fail: set[str] = set()
    transport = make_transport(client, fail)
    w.pair("http://test", code, "laptop", state, adapters, transport=transport)
    cfg = state.load()
    worker = w.Worker(w.Api("http://test", cfg["device_token"], transport=transport), state, adapters, sleep=lambda s: None)
    worker.device_heartbeat(force=True)
    return bob, cfg["device_id"], worker, state, fail


def test_worker_executes_and_settles(relay, paired_worker):
    bob, dev_id, worker, state, _ = paired_worker
    alice = relay.login("alice")
    t = relay.create(alice, prompt="Say hi.\n--- ECHO_OUTPUT ---\nhello from the relay\n---").json()["task_id"]
    assert relay.claim(bob, t, dev_id).status_code == 200
    res = worker.poll_once()
    assert res["outcome"] == "succeeded" and res["fee_milli"] > 0
    task = relay.c.get(f"/v2/tasks/{t}", headers=alice).json()
    assert task["status"] == "done" and task["runs"][0]["output_text"] == "hello from the relay"
    assert state.pending_receipts() == []
    assert relay.reconcile()["ok"]


def test_receipt_survives_dropped_finish_and_is_not_reexecuted(relay, paired_worker, monkeypatch):
    bob, dev_id, worker, state, fail = paired_worker
    alice = relay.login("alice")
    t = relay.create(alice).json()["task_id"]
    relay.claim(bob, t, dev_id)

    calls = []
    real = EchoAdapter.execute
    monkeypatch.setattr(EchoAdapter, "execute", lambda self, *a: calls.append(1) or real(self, *a))
    fail.add("/finish")
    assert worker.poll_once() is None  # upload dropped
    assert len(state.pending_receipts()) == 1
    assert worker.poll_once() is None  # flushes the saved receipt; does not call the model again
    assert calls == [1]
    assert state.pending_receipts() == []
    assert relay.c.get(f"/v2/tasks/{t}", headers=alice).json()["status"] == "done"
    bal = relay.me(bob)["account"]["balance_milli"]
    worker.flush_receipts()
    assert relay.me(bob)["account"]["balance_milli"] == bal
    assert relay.reconcile()["ok"]


def test_single_instance_lock(tmp_path):
    p = tmp_path / "worker.lock"
    with w.SingleInstanceLock(p):
        with pytest.raises(SystemExit):
            with w.SingleInstanceLock(p):
                pass


def test_render_and_hash_parity_with_server():
    tpl = TEMPLATES["research"]
    spec = server_spec.build_spec(
        category="research", template_version="t", title="标题", prompt=tpl["prompt"],
        inputs=[{"name": "a.txt", "text": "材料 ✓"}], deliverable=tpl["deliverable"],
        deliverable_schema=tpl["deliverable_schema"], adapter="echo", model="echo-1", required_tools=[],
        budget_milli=1000, max_runtime_s=60, validator_id="json_schema", validator_version="1",
        validator_params={"schema": tpl["deliverable_schema"]},
    )
    assert w.render_prompt(spec) == server_spec.render_prompt(spec)
    assert w.spec_hash(spec) == server_spec.spec_hash(spec)


def test_claude_parser():
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                                "output_tokens": 7}})
    r = ClaudeCodeAdapter.parse(out, 0, None)
    assert r.status == "succeeded" and r.output_text == "done"
    assert (r.usage["input_tokens"], r.usage["output_tokens"]) == (115, 7)
    assert ClaudeCodeAdapter.parse("garbage", 1, None).usage["status"] == "unknown"
    assert ClaudeCodeAdapter.parse(out, 0, "TIMEOUT").status == "failed"


def test_codex_parser():
    lines = [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 40}},
    ]
    r = CodexAdapter.parse("\n".join(json.dumps(x) for x in lines), 0, None)
    assert r.status == "succeeded" and r.output_text == "answer"
    assert (r.usage["input_tokens"], r.usage["output_tokens"]) == (300, 40)  # cached already inside input
    assert CodexAdapter.parse("", 0, None).usage["status"] == "unknown"


def test_http_parser():
    r = HttpAdapter.parse({"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    assert r.status == "succeeded" and r.usage["input_tokens"] == 3
    assert HttpAdapter.parse({"choices": [{"message": {"content": "x"}}]}).usage["status"] == "unknown"


def test_echo_respects_cancel(tmp_path):
    ev = threading.Event()
    ev.set()
    assert EchoAdapter().execute("p", "echo-1", str(tmp_path), 5, ev).error_code == "CANCELLED"


def test_insecure_server_url_refused():
    w.check_server_url("https://relay.example.org")
    w.check_server_url("http://127.0.0.1:8787")
    with pytest.raises(SystemExit):
        w.check_server_url("http://relay.example.org")
