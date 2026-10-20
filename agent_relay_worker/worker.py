"""Local worker: pair, poll, run under a lease, persist receipts, settle.

Guarantees it aims for (section 07):
  * one worker process per state dir (file lock);
  * no model call before the server grants the run (start);
  * a receipt is written to disk before upload and re-sent until the server
    answers definitively; a run with a saved receipt is never re-executed;
  * stop requests and lost leases cancel the local process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import PROTOCOL_VERSION, __version__
from .adapters import Adapter, ExecResult

log = logging.getLogger("agent_relay_worker")

IDLE_POLL_S = 8
DEVICE_HEARTBEAT_S = 30
RUN_HEARTBEAT_S = 20
DEFINITIVE_ERRORS = {"LEASE_EXPIRED", "IDEMPOTENCY_CONFLICT", "FORBIDDEN", "NOT_FOUND", "ARTIFACT_INVALID", "DEVICE_REVOKED"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status >= 500 or self.status == 429


class Api:
    """Minimal JSON-over-HTTPS client. Tests swap `transport`."""

    def __init__(self, server: str, token: str | None = None, transport: Callable | None = None):
        self.server = server.rstrip("/")
        self.token = token
        self.transport = transport or self._urllib

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": f"agent-relay-worker/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status, data = self.transport(method, self.server + path, headers, body)
        if status >= 400:
            err = (data or {}).get("error") or {}
            raise ApiError(status, err.get("code", f"HTTP_{status}"), err.get("message", ""))
        return data

    @staticmethod
    def _urllib(method, url, headers, body):
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ApiError(0, "NETWORK", str(e)) from e


# --------------------------------------------------------------------------- spec helpers (mirror relay.spec)

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def render_prompt(spec: dict) -> str:
    parts = [spec["prompt"].strip()]
    for item in spec.get("input_manifest") or []:
        parts.append(f"\n\n--- 输入材料: {item['name']} ---\n{item['text']}")
    if spec.get("deliverable"):
        parts.append(f"\n\n--- 交付要求 ---\n{spec['deliverable']}")
    if spec.get("deliverable_schema"):
        parts.append("\n\n只输出满足以下 JSON Schema 的 JSON，不要输出其他文字：\n" + canonical_json(spec["deliverable_schema"]))
    return "".join(parts)


# --------------------------------------------------------------------------- state

class State:
    def __init__(self, root: Path):
        self.root = root
        self.receipts = root / "receipts"
        self.rejected = root / "receipts" / "rejected"
        self.rejected.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    def load(self) -> dict:
        return json.loads(self.config_path.read_text()) if self.config_path.exists() else {}

    def save(self, cfg: dict) -> None:
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.config_path)

    def receipt_path(self, run_id: str) -> Path:
        return self.receipts / f"{run_id}.json"

    def write_receipt(self, run_id: str, record: dict) -> None:
        p = self.receipt_path(run_id)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(p)

    def pending_receipts(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.receipts.glob("*.json"))]

    def drop_receipt(self, run_id: str, rejected: bool = False) -> None:
        p = self.receipt_path(run_id)
        if not p.exists():
            return
        if rejected:
            p.replace(self.rejected / p.name)
        else:
            p.unlink()


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.fh.close()
            raise SystemExit(f"Another worker is already running with state dir {self.path.parent}") from e
        return self

    def __exit__(self, *exc):
        self.fh.close()


# --------------------------------------------------------------------------- worker

class Worker:
    def __init__(self, api: Api, state: State, adapters: dict[str, Adapter], sleep: Callable[[float], None] = time.sleep):
        self.api = api
        self.state = state
        self.adapters = adapters
        self.sleep = sleep
        self._last_device_hb = 0.0

    def capabilities(self) -> dict:
        caps = {}
        for name, a in self.adapters.items():
            c = a.capabilities()
            if c is not None:
                caps[name] = c
        return {"adapters": caps, "tools": []}

    def device_heartbeat(self, force: bool = False) -> None:
        if force or time.monotonic() - self._last_device_hb >= DEVICE_HEARTBEAT_S:
            self.api.call("POST", "/v2/devices/heartbeat",
                          {"ready": True, "capabilities": self.capabilities(), "client_version": __version__})
            self._last_device_hb = time.monotonic()

    def flush_receipts(self) -> None:
        for rec in self.state.pending_receipts():
            self.submit(rec)

    def submit(self, rec: dict) -> dict | None:
        try:
            res = self.api.call("POST", f"/v2/runs/{rec['run_id']}/finish",
                                {"fencing_token": rec["fencing_token"], "receipt": rec["receipt"]})
        except ApiError as e:
            if e.retryable:
                log.warning("receipt upload deferred run=%s err=%s", rec["run_id"], e.code)
                return None
            log.error("receipt rejected run=%s err=%s", rec["run_id"], e.code)
            self.state.drop_receipt(rec["run_id"], rejected=True)
            return None
        self.state.drop_receipt(rec["run_id"])
        log.info("settled run=%s outcome=%s fee_milli=%s", rec["run_id"], res["outcome"], res["fee_milli"])
        return res

    def poll_once(self) -> dict | None:
        """One iteration: heartbeat, flush, maybe execute. Returns finish result if a run settled."""
        self.device_heartbeat()
        self.flush_receipts()
        run = self.api.call("GET", "/v2/runs/next")["run"]
        if run is None:
            return None
        if self.state.receipt_path(run["run_id"]).exists():
            return None  # already executed; upload pending — never call the model twice
        return self.execute(run)

    def execute(self, run: dict) -> dict | None:
        run_id, token, spec = run["run_id"], run["fencing_token"], run["spec"]
        try:
            self.api.call("POST", f"/v2/runs/{run_id}/start", {"fencing_token": token})
        except ApiError as e:
            log.warning("could not start run=%s err=%s", run_id, e.code)
            return None

        cancel = threading.Event()
        stop_hb = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop, args=(run_id, token, cancel, stop_hb), daemon=True)
        hb.start()
        try:
            result = self._run_adapter(run, spec, cancel)
        finally:
            stop_hb.set()
            hb.join(timeout=5)

        receipt = {
            "status": result.status,
            "spec_hash": run["spec_hash"],
            "output_text": result.output_text,
            "error_code": result.error_code,
            "usage": result.usage,
            "adapter": {"name": spec["adapter"], "client_version": __version__},
        }
        rec = {"run_id": run_id, "task_id": run["task_id"], "fencing_token": token, "receipt": receipt}
        self.state.write_receipt(run_id, rec)
        return self.submit(rec)

    def _run_adapter(self, run: dict, spec: dict, cancel: threading.Event) -> ExecResult:
        if spec_hash(spec) != run["spec_hash"]:
            return ExecResult("failed", error_code="SPEC_HASH_MISMATCH")
        adapter = self.adapters.get(spec["adapter"])
        if adapter is None or adapter.capabilities() is None:
            return ExecResult("failed", error_code="ADAPTER_UNAVAILABLE")
        with tempfile.TemporaryDirectory(prefix="relay-run-") as workdir:
            try:
                return adapter.execute(render_prompt(spec), spec["model"], workdir, int(spec["max_runtime_s"]), cancel)
            except Exception as e:  # adapter bug must not lose the run
                log.exception("adapter crashed run=%s", run["run_id"])
                return ExecResult("failed", error_code=f"ADAPTER_CRASH_{type(e).__name__}"[:64])

    def _heartbeat_loop(self, run_id: str, token: int, cancel: threading.Event, stop: threading.Event) -> None:
        while not stop.wait(RUN_HEARTBEAT_S):
            try:
                res = self.api.call("POST", f"/v2/runs/{run_id}/heartbeat", {"fencing_token": token})
                if res.get("stop_requested"):
                    log.info("stop requested run=%s", run_id)
                    cancel.set()
            except ApiError as e:
                if e.code in ("LEASE_EXPIRED", "DEVICE_REVOKED", "FORBIDDEN"):
                    log.warning("lost run lease run=%s err=%s", run_id, e.code)
                    cancel.set()
                    return

    def run_forever(self) -> None:
        backoff = 1.0
        log.info("worker %s online; adapters=%s", __version__, sorted(self.capabilities()["adapters"]))
        while True:
            try:
                settled = self.poll_once()
                backoff = 1.0
                if settled is None:
                    self.sleep(IDLE_POLL_S + random.uniform(0, 3))
            except ApiError as e:
                if e.code == "DEVICE_REVOKED":
                    raise SystemExit("Device key revoked; pair again.")
                log.warning("poll failed err=%s; retry in %.0fs", e.code, backoff)
                self.sleep(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 120)


def check_server_url(server: str) -> None:
    """The device key travels in every request; refuse cleartext except to this machine."""
    from urllib.parse import urlparse
    u = urlparse(server)
    if u.scheme == "https":
        return
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1", "test"):
        return
    raise SystemExit(f"Refusing insecure server URL {server!r}; use https://")


def pair(server: str, code: str, name: str, state: State, adapters: dict[str, Adapter],
         transport: Callable | None = None) -> dict:
    check_server_url(server)
    tmp = Worker(Api(server, transport=transport), state, adapters)
    res = Api(server, transport=transport).call("POST", "/v2/devices/pair", {
        "code": code, "name": name, "capabilities": tmp.capabilities(),
        "client_version": __version__, "protocol_version": PROTOCOL_VERSION,
    })
    state.save({"server": server.rstrip("/"), "device_id": res["device_id"], "device_token": res["device_token"]})
    return res
