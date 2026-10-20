from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay import config
from relay.api import create_app
from relay.config import Settings

ECHO_CAPS = {"adapters": {"echo": {"models": ["echo-1"], "usage": "known"}}, "tools": []}


class Clock:
    def __init__(self, t: int = 1_800_000_000):
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, s: int) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(config, "now", c)
    return c


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "relay.db"), allow_dev_login=True, admin_token="admin-secret",
                    allow_test_adapters=True, rate_limits=False)


@pytest.fixture
def client(settings, clock):
    return TestClient(create_app(settings))


class Relay:
    """Thin test DSL over the HTTP API."""

    def __init__(self, client: TestClient):
        self.c = client

    def login(self, handle: str) -> dict:
        r = self.c.post("/v2/auth/dev-login", json={"handle": handle})
        assert r.status_code == 200, r.text
        self.c.cookies.clear()  # tests authenticate with explicit bearer headers
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def pair(self, user: dict, caps: dict | None = None, ready: bool = True) -> tuple[str, dict]:
        code = self.c.post("/v2/devices/pair-codes", headers=user).json()["code"]
        r = self.c.post("/v2/devices/pair", json={"code": code, "name": "laptop", "capabilities": caps or ECHO_CAPS,
                                                  "client_version": "test"})
        assert r.status_code == 200, r.text
        dev = {"Authorization": f"Bearer {r.json()['device_token']}"}
        if ready:
            assert self.c.post("/v2/devices/heartbeat", headers=dev, json={"ready": True}).status_code == 200
        return r.json()["device_id"], dev

    def create(self, user: dict, budget_rp: int = 10, key: str | None = None, **over) -> dict:
        body = {"category": "blog", "title": "Test post", "prompt": "Write.", "adapter": "echo", "model": "echo-1",
                "budget_milli": budget_rp * 1000, "public_consent": True,
                "validator": {"id": "nonempty", "params": {}}}
        body.update(over)
        headers = dict(user, **({"Idempotency-Key": key} if key else {}))
        return self.c.post("/v2/tasks", headers=headers, json=body)

    def claim(self, user: dict, task_id: str, device_id: str):
        return self.c.post(f"/v2/tasks/{task_id}/claim", headers=user, json={"device_id": device_id})

    def next(self, dev: dict) -> dict | None:
        return self.c.get("/v2/runs/next", headers=dev).json()["run"]

    def start(self, dev: dict, run: dict):
        return self.c.post(f"/v2/runs/{run['run_id']}/start", headers=dev, json={"fencing_token": run["fencing_token"]})

    def finish(self, dev: dict, run: dict, *, status="succeeded", output="hello world", usage=None, token=None):
        receipt = {"status": status, "spec_hash": run["spec_hash"], "output_text": output,
                   "usage": usage or {"status": "known", "input_tokens": 2000, "output_tokens": 1000}}
        return self.c.post(f"/v2/runs/{run['run_id']}/finish", headers=dev,
                           json={"fencing_token": token if token is not None else run["fencing_token"], "receipt": receipt})

    def me(self, user: dict) -> dict:
        return self.c.get("/v2/me", headers=user).json()

    def reconcile(self) -> dict:
        return self.c.get("/v2/admin/reconcile", headers={"Authorization": "Bearer admin-secret"}).json()


@pytest.fixture
def relay(client):
    return Relay(client)
