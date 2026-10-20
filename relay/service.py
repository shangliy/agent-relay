"""Control-plane business logic.

Every state change runs inside one BEGIN IMMEDIATE transaction together with
its audit event, and every conditional UPDATE checks affected rows so the API
never reports a success that did not happen (section 12).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import config, errors
from .config import Settings
from .db import tx
from .spec import build_spec, canonical_json, render_prompt, sha256_hex, spec_hash
from .templates import TEMPLATE_VERSION, TEMPLATES
from .validators import VALIDATORS, check_params, run_validator

ACTIVE_RUN_STATES = ("assigned", "running", "uploading", "validating")
RESERVED_TASK_STATES = ("open", "claimed", "stalled")
_ACTIVE_SQL = "('assigned','running','uploading','validating')"


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _event(conn: sqlite3.Connection, actor: str, entity: str, entity_id: str, kind: str, **data: Any) -> None:
    conn.execute(
        "INSERT INTO events(actor, entity, entity_id, kind, data_json, created_at) VALUES (?,?,?,?,?,?)",
        (actor, entity, entity_id, kind, canonical_json(data), config.now()),
    )


def _changed(cur: sqlite3.Cursor, err: errors.RelayError) -> None:
    if cur.rowcount != 1:
        raise err


def compute_fee_milli(budget_milli: int, input_tokens: int, output_tokens: int) -> int:
    return min(budget_milli, input_tokens * config.INPUT_TOKEN_MILLI + output_tokens * config.OUTPUT_TOKEN_MILLI)


# --------------------------------------------------------------------------- idempotency

def _idem_get(conn, scope: str, principal: str, key: str | None, request: Any) -> dict | None:
    if not key:
        return None
    row = conn.execute(
        "SELECT request_hash, response_json FROM idempotency WHERE scope=? AND principal=? AND key=?",
        (scope, principal, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != sha256_hex(canonical_json(request)):
        raise errors.idempotency_conflict()
    return json.loads(row["response_json"])


def _idem_put(conn, scope: str, principal: str, key: str | None, request: Any, response: dict) -> None:
    if key:
        conn.execute(
            "INSERT INTO idempotency(scope, principal, key, request_hash, response_json, created_at) VALUES (?,?,?,?,?,?)",
            (scope, principal, key, sha256_hex(canonical_json(request)), canonical_json(response), config.now()),
        )


# --------------------------------------------------------------------------- identity

@dataclass
class Account:
    id: str
    handle: str


@dataclass
class Device:
    id: str
    account_id: str
    capabilities: dict


def _new_account(conn, handle: str, github_id: int | None = None) -> str:
    account_id = _id("acct")
    conn.execute(
        "INSERT INTO accounts(id, handle, github_id, balance_milli, reserved_milli, credit_milli, created_at) VALUES (?,?,?,?,?,?,?)",
        (account_id, handle, github_id, config.INITIAL_BALANCE_MILLI, 0, config.CREDIT_LIMIT_MILLI, config.now()),
    )
    _event(conn, account_id, "account", account_id, "created", github_id=github_id)
    return account_id


def _new_session(conn, account_id: str) -> str:
    token = "u_" + secrets.token_urlsafe(24)
    now = config.now()
    conn.execute(
        "INSERT INTO sessions(token_hash, account_id, created_at, expires_at) VALUES (?,?,?,?)",
        (sha256_hex(token), account_id, now, now + config.SESSION_TTL_S),
    )
    return token


def dev_login(conn, settings: Settings, handle: str) -> dict:
    if not settings.allow_dev_login:
        raise errors.forbidden("Dev login is disabled")
    handle = handle.strip().lower()
    if not (2 <= len(handle) <= 32) or not all(c.isalnum() or c in "-_" for c in handle):
        raise errors.invalid_input("handle must be 2-32 chars of [a-z0-9_-]")
    with tx(conn):
        row = conn.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()
        account_id = row["id"] if row else _new_account(conn, handle)
        token = _new_session(conn, account_id)
    return {"token": token, "account_id": account_id, "handle": handle}


def github_login(conn, settings: Settings, github_id: int, login: str, invite_code: str | None) -> dict:
    """Existing GitHub accounts sign in freely; new ones need an invite during the pilot."""
    with tx(conn):
        row = conn.execute("SELECT id, handle FROM accounts WHERE github_id=?", (github_id,)).fetchone()
        if row:
            return {"token": _new_session(conn, row["id"]), "account_id": row["id"], "handle": row["handle"], "new": False}
        if not settings.open_signup:
            code = (invite_code or "").strip().upper()
            cur = conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE code_hash=? AND uses < max_uses AND (expires_at IS NULL OR expires_at >= ?)",
                (sha256_hex(code), config.now()),
            )
            _changed(cur, errors.RelayError("INVITE_REQUIRED", "A valid invite code is required to join the pilot", 403,
                                            "Ask the organizers for an invite code."))
        handle = login.lower()[:32]
        if conn.execute("SELECT 1 FROM accounts WHERE handle=?", (handle,)).fetchone():
            handle = f"{handle[:24]}-{secrets.token_hex(3)}"
        account_id = _new_account(conn, handle, github_id)
        return {"token": _new_session(conn, account_id), "account_id": account_id, "handle": handle, "new": True}


def logout(conn, token: str | None) -> None:
    if token:
        with tx(conn):
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (sha256_hex(token),))


def create_invites(conn, n: int, max_uses: int, note: str, ttl_days: int | None) -> list[str]:
    if not (1 <= n <= 200 and 1 <= max_uses <= 1000):
        raise errors.invalid_input("n must be 1..200 and max_uses 1..1000")
    expires = config.now() + ttl_days * 86400 if ttl_days else None
    codes = []
    with tx(conn):
        for _ in range(n):
            raw = secrets.token_hex(5).upper()
            code = f"{raw[:5]}-{raw[5:]}"
            conn.execute(
                "INSERT INTO invites(code_hash, note, max_uses, expires_at, created_at) VALUES (?,?,?,?,?)",
                (sha256_hex(code), note[:120], max_uses, expires, config.now()),
            )
            codes.append(code)
        _event(conn, "admin", "invites", "-", "created", n=n, max_uses=max_uses, note=note[:120])
    return codes


def account_from_token(conn, token: str | None) -> Account:
    if not token or not token.startswith("u_"):
        raise errors.auth_required()
    row = conn.execute(
        """SELECT a.id, a.handle FROM sessions s JOIN accounts a ON a.id = s.account_id
           WHERE s.token_hash=? AND (s.expires_at IS NULL OR s.expires_at >= ?)""",
        (sha256_hex(token), config.now()),
    ).fetchone()
    if row is None:
        raise errors.auth_required()
    return Account(row["id"], row["handle"])


def device_from_token(conn, token: str | None) -> Device:
    """Pure authentication — does not touch last_seen (heartbeat is explicit)."""
    if not token or not token.startswith("d_"):
        raise errors.auth_required("Device key required")
    row = conn.execute(
        "SELECT id, account_id, capabilities_json, revoked_at FROM devices WHERE token_hash=?",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        raise errors.auth_required("Unknown device key")
    if row["revoked_at"] is not None:
        raise errors.device_revoked()
    return Device(row["id"], row["account_id"], json.loads(row["capabilities_json"]))


# --------------------------------------------------------------------------- devices

def create_pair_code(conn, account: Account) -> dict:
    raw = secrets.token_hex(4).upper()
    code = f"{raw[:4]}-{raw[4:]}"
    expires = config.now() + config.PAIR_CODE_TTL_S
    with tx(conn):
        conn.execute(
            "INSERT INTO pair_codes(code_hash, account_id, expires_at) VALUES (?,?,?)",
            (sha256_hex(code), account.id, expires),
        )
        _event(conn, account.id, "account", account.id, "pair_code_created")
    return {"code": code, "expires_at": expires}


def _validate_capabilities(caps: Any) -> dict:
    if not isinstance(caps, dict):
        raise errors.invalid_input("capabilities must be an object")
    adapters = caps.get("adapters", {})
    if not isinstance(adapters, dict):
        raise errors.invalid_input("capabilities.adapters must be an object")
    for name, info in adapters.items():
        if not isinstance(info, dict) or not isinstance(info.get("models", []), list):
            raise errors.invalid_input(f"capabilities.adapters.{name} malformed")
    tools = caps.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise errors.invalid_input("capabilities.tools must be a list of strings")
    return {"adapters": adapters, "tools": tools}


def pair_device(conn, code: str, name: str, capabilities: dict, client_version: str, protocol_version: int) -> dict:
    caps = _validate_capabilities(capabilities)
    token = "d_" + secrets.token_urlsafe(32)
    device_id = _id("dev")
    now = config.now()
    with tx(conn):
        row = conn.execute(
            "SELECT account_id, expires_at, used_at FROM pair_codes WHERE code_hash=?",
            (sha256_hex(code.strip().upper()),),
        ).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] < now:
            raise errors.RelayError("PAIR_CODE_INVALID", "Pair code is invalid, used or expired", 400, "Generate a new code on the web.")
        _changed(
            conn.execute("UPDATE pair_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL", (now, sha256_hex(code.strip().upper()))),
            errors.RelayError("PAIR_CODE_INVALID", "Pair code already used", 400),
        )
        conn.execute(
            """INSERT INTO devices(id, account_id, token_hash, name, client_version, protocol_version,
                                   capabilities_json, ready, last_seen_at, created_at)
               VALUES (?,?,?,?,?,?,?,0,?,?)""",
            (device_id, row["account_id"], sha256_hex(token), name[:64] or "device", client_version[:32],
             protocol_version, canonical_json(caps), now, now),
        )
        _event(conn, row["account_id"], "device", device_id, "paired", client_version=client_version)
    return {"device_id": device_id, "device_token": token, "account_id": row["account_id"]}


def device_heartbeat(conn, device: Device, ready: bool, capabilities: dict | None, client_version: str | None) -> dict:
    caps = _validate_capabilities(capabilities) if capabilities is not None else None
    with tx(conn):
        conn.execute(
            """UPDATE devices SET ready=?, last_seen_at=?,
                   capabilities_json=COALESCE(?, capabilities_json),
                   client_version=COALESCE(?, client_version)
               WHERE id=? AND revoked_at IS NULL""",
            (1 if ready else 0, config.now(), canonical_json(caps) if caps is not None else None, client_version, device.id),
        )
    return {"ok": True, "server_time": config.now()}


def revoke_device(conn, account: Account, device_id: str) -> dict:
    with tx(conn):
        cur = conn.execute(
            "UPDATE devices SET revoked_at=?, ready=0 WHERE id=? AND account_id=? AND revoked_at IS NULL",
            (config.now(), device_id, account.id),
        )
        _changed(cur, errors.not_found("Active device"))
        _event(conn, account.id, "device", device_id, "revoked")
    return {"ok": True}


def _device_ready(row: sqlite3.Row) -> bool:
    return (
        row["revoked_at"] is None
        and bool(row["ready"])
        and row["last_seen_at"] is not None
        and config.now() - row["last_seen_at"] <= config.DEVICE_READY_WINDOW_S
    )


def _capability_problem(caps: dict, adapter: str, model: str, required_tools: list[str]) -> str | None:
    info = caps.get("adapters", {}).get(adapter)
    if info is None:
        return f"device has no {adapter!r} adapter"
    models = info.get("models") or []
    if "*" not in models and model not in models:
        return f"adapter {adapter!r} on this device does not offer model {model!r}"
    if info.get("usage") != "known":
        return f"adapter {adapter!r} cannot report token usage; credit tasks need known usage"
    missing = set(required_tools) - set(caps.get("tools", []))
    if missing:
        return f"device lacks tools: {', '.join(sorted(missing))}"
    return None


# --------------------------------------------------------------------------- tasks

def create_task(conn, settings: Settings, account: Account, req: dict, idem_key: str | None) -> dict:
    if not settings.allow_new_tasks:
        raise errors.disabled("Publishing new tasks")
    category = req["category"]
    template = TEMPLATES.get(category)
    if template is None:
        raise errors.invalid_input(f"unknown category {category!r}")
    if req.get("public_consent") is not True:
        raise errors.invalid_input("public_consent must be true: inputs and results are public")
    budget_milli = int(req["budget_milli"])
    if not (config.MIN_BUDGET_MILLI <= budget_milli <= config.MAX_BUDGET_MILLI):
        raise errors.invalid_input("budget must be between 1 and 100 RP")
    max_runtime = int(req.get("max_runtime_s") or config.DEFAULT_MAX_RUNTIME_S)
    if not (30 <= max_runtime <= config.MAX_MAX_RUNTIME_S):
        raise errors.invalid_input(f"max_runtime_s must be 30..{config.MAX_MAX_RUNTIME_S}")
    validator = req.get("validator") or template["validator"]
    validator_id, validator_params = validator["id"], validator.get("params") or {}
    try:
        check_params(validator_id, validator_params)
    except Exception as e:
        raise errors.invalid_input(f"validator: {e}") from e
    deliverable_schema = req.get("deliverable_schema")
    if deliverable_schema is None and validator_id == "json_schema":
        deliverable_schema = validator_params["schema"]
    prompt = (req.get("prompt") or template["prompt"]).strip()
    if not prompt:
        raise errors.invalid_input("prompt is empty")

    spec = build_spec(
        category=category,
        template_version=TEMPLATE_VERSION,
        title=req["title"].strip()[:120],
        prompt=prompt,
        inputs=[{"name": i["name"][:120], "text": i["text"]} for i in req.get("inputs") or []],
        deliverable=(req.get("deliverable") or template["deliverable"]).strip(),
        deliverable_schema=deliverable_schema,
        adapter=req["adapter"],
        model=req["model"],
        required_tools=req.get("required_tools") or [],
        budget_milli=budget_milli,
        max_runtime_s=max_runtime,
        validator_id=validator_id,
        validator_version=VALIDATORS[validator_id][0],
        validator_params=validator_params,
    )
    if not spec["title"]:
        raise errors.invalid_input("title is empty")
    if spec["adapter"] == "echo" and not settings.allow_test_adapters:
        raise errors.invalid_input("the echo adapter is for local testing only")
    if spec["required_tools"]:
        # Tool permissions need host-side isolation (section 10) — not shipped yet.
        raise errors.invalid_input("tool-using tasks are not enabled yet")

    with tx(conn):
        replay = _idem_get(conn, "create_task", account.id, idem_key, req)
        if replay is not None:
            return replay
        acct = conn.execute("SELECT balance_milli, reserved_milli, credit_milli FROM accounts WHERE id=?", (account.id,)).fetchone()
        if acct["balance_milli"] < 0:
            raise errors.insufficient_credit("Balance is negative; complete tasks to get back to zero first")
        available = acct["balance_milli"] - acct["reserved_milli"] + acct["credit_milli"]
        if available < budget_milli:
            raise errors.insufficient_credit(f"Available {available} milli RP < budget {budget_milli}")
        task_id = _id("task")
        now = config.now()
        conn.execute(
            """INSERT INTO tasks(id, publisher_id, category, title, schema_version, spec_json, spec_hash,
                                 adapter, model, budget_milli, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            (task_id, account.id, category, spec["title"], spec["schema_version"], canonical_json(spec),
             spec_hash(spec), spec["adapter"], spec["model"], budget_milli, now, now),
        )
        conn.execute("UPDATE accounts SET reserved_milli = reserved_milli + ? WHERE id=?", (budget_milli, account.id))
        _event(conn, account.id, "task", task_id, "created", budget_milli=budget_milli, spec_hash=spec_hash(spec))
        resp = {"task_id": task_id, "spec_hash": spec_hash(spec), "status": "open", "budget_milli": budget_milli}
        _idem_put(conn, "create_task", account.id, idem_key, req, resp)
    return resp


def _pick_device(conn, account: Account, task: sqlite3.Row, spec: dict) -> sqlite3.Row:
    """One-tap claim from a phone: choose the most recently seen ready, idle,
    compatible device. Errors name the most actionable reason."""
    devices = conn.execute(
        "SELECT * FROM devices WHERE account_id=? AND revoked_at IS NULL ORDER BY last_seen_at DESC", (account.id,)
    ).fetchall()
    if not devices:
        raise errors.RelayError("NO_DEVICE", "No computer is connected to your account", 409,
                                "Open the Devices tab and connect a computer first.")
    ready = [d for d in devices if _device_ready(d)]
    if not ready:
        raise errors.device_not_ready()
    problems = []
    for d in ready:
        problem = _capability_problem(json.loads(d["capabilities_json"]), task["adapter"], task["model"], spec["required_tools"])
        if problem:
            problems.append(problem)
            continue
        if conn.execute(f"SELECT 1 FROM runs WHERE device_id=? AND state IN {_ACTIVE_SQL}", (d["id"],)).fetchone():
            problems.append("busy")
            continue
        return d
    if "busy" in problems:
        raise errors.invalid_state("Your compatible computer is already running a task; wait for it to finish")
    raise errors.capability_mismatch(next(p for p in problems if p != "busy"))


def claim_task(conn, settings: Settings, account: Account, task_id: str, device_id: str | None, idem_key: str | None) -> dict:
    if not settings.allow_new_claims:
        raise errors.disabled("Claiming tasks")
    req = {"task_id": task_id, "device_id": device_id}
    sweep_expired(conn)
    with tx(conn):
        replay = _idem_get(conn, "claim", account.id, idem_key, req)
        if replay is not None:
            return replay
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise errors.not_found("Task")
        if task["status"] != "open":
            raise errors.task_already_claimed()
        if task["publisher_id"] == account.id:
            raise errors.forbidden("You cannot claim your own task")
        spec = json.loads(task["spec_json"])
        if device_id is None:
            dev = _pick_device(conn, account, task, spec)
            device_id = dev["id"]
        else:
            dev = conn.execute("SELECT * FROM devices WHERE id=? AND account_id=?", (device_id, account.id)).fetchone()
            if dev is None:
                raise errors.not_found("Device")
            if dev["revoked_at"] is not None:
                raise errors.device_revoked()
            if not _device_ready(dev):
                raise errors.device_not_ready()
            problem = _capability_problem(json.loads(dev["capabilities_json"]), task["adapter"], task["model"], spec["required_tools"])
            if problem:
                raise errors.capability_mismatch(problem)
            busy = conn.execute(f"SELECT 1 FROM runs WHERE device_id=? AND state IN {_ACTIVE_SQL}", (device_id,)).fetchone()
            if busy:
                raise errors.invalid_state("This device already has an active run")

        now = config.now()
        run_id = _id("run")
        fencing = task["fencing_counter"] + 1
        attempt = task["attempts"] + 1
        _changed(
            conn.execute(
                """UPDATE tasks SET status='claimed', fencing_counter=?, attempts=?, active_run_id=?, updated_at=?
                   WHERE id=? AND status='open' AND fencing_counter=?""",
                (fencing, attempt, run_id, now, task_id, task["fencing_counter"]),
            ),
            errors.task_already_claimed(),
        )
        conn.execute(
            """INSERT INTO runs(id, task_id, device_id, worker_id, attempt, fencing_token, state,
                                lease_until, lease_hard_limit, created_at)
               VALUES (?,?,?,?,?,?,'assigned',?,?,?)""",
            (run_id, task_id, device_id, account.id, attempt, fencing, now + config.ASSIGN_LEASE_S,
             now + config.ASSIGN_LEASE_S + spec["max_runtime_s"] + config.LEASE_GRACE_S, now),
        )
        _event(conn, account.id, "task", task_id, "claimed", run_id=run_id, device_id=device_id, fencing_token=fencing)
        resp = {"task_id": task_id, "run_id": run_id, "fencing_token": fencing, "state": "assigned",
                "device_id": device_id, "device_name": dev["name"]}
        _idem_put(conn, "claim", account.id, idem_key, req, resp)
    return resp


def cancel_task(conn, account: Account, task_id: str) -> dict:
    """Only tasks with no active run can be cancelled; running work is stopped via stop_run."""
    sweep_expired(conn)
    with tx(conn):
        task = conn.execute("SELECT publisher_id, budget_milli, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise errors.not_found("Task")
        if task["publisher_id"] != account.id:
            raise errors.forbidden("Only the publisher can cancel")
        _changed(
            conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status IN ('open','stalled')",
                (config.now(), task_id),
            ),
            errors.invalid_state(f"Task is {task['status']}; only open or stalled tasks can be cancelled"),
        )
        conn.execute("UPDATE accounts SET reserved_milli = reserved_milli - ? WHERE id=?", (task["budget_milli"], account.id))
        _event(conn, account.id, "task", task_id, "cancelled", released_milli=task["budget_milli"])
    return {"task_id": task_id, "status": "cancelled"}


def reopen_task(conn, account: Account, task_id: str, acknowledge_duplicate_cost: bool) -> dict:
    """A stalled task goes back to the pool only on explicit re-authorization:
    the expired run may still be executing at the provider (section 07)."""
    if not acknowledge_duplicate_cost:
        raise errors.invalid_input("Reopening may cause a duplicate model call; set acknowledge_duplicate_cost=true")
    with tx(conn):
        task = conn.execute("SELECT publisher_id, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise errors.not_found("Task")
        if task["publisher_id"] != account.id:
            raise errors.forbidden("Only the publisher can reopen")
        _changed(
            conn.execute("UPDATE tasks SET status='open', updated_at=? WHERE id=? AND status='stalled'", (config.now(), task_id)),
            errors.invalid_state(f"Task is {task['status']}; only stalled tasks can be reopened"),
        )
        _event(conn, account.id, "task", task_id, "reopened", acknowledged_duplicate_cost=True)
    return {"task_id": task_id, "status": "open"}


_SUMMARY_COLS = "id, publisher_id, category, title, adapter, model, budget_milli, status, created_at"


def list_tasks(conn, status: str | None, limit: int, before: str | None, publisher_id: str | None = None) -> dict:
    """Summaries only, keyset-paginated; details are fetched per task."""
    sweep_expired(conn)
    limit = max(1, min(limit, 50))
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if publisher_id:
        where.append("publisher_id=?")
        args.append(publisher_id)
    if before:
        try:
            ts, tid = before.split(":", 1)
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            args += [int(ts), int(ts), tid]
        except ValueError as e:
            raise errors.invalid_input("bad cursor") from e
    sql = f"SELECT {_SUMMARY_COLS} FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(sql, (*args, limit + 1))]
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = f"{rows[-1]['created_at']}:{rows[-1]['id']}" if more else None
    return {"tasks": rows, "next_cursor": cursor}


def get_task(conn, task_id: str) -> dict:
    sweep_expired(conn)
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise errors.not_found("Task")
    runs = []
    for r in conn.execute(
        """SELECT id, device_id, worker_id, attempt, state, outcome, usage_status, input_tokens, output_tokens,
                  fee_milli, error_code, output_text, created_at, started_at, finished_at
           FROM runs WHERE task_id=? ORDER BY attempt""",
        (task_id,),
    ):
        run = dict(r)
        v = conn.execute(
            "SELECT validator_id, validator_version, passed, report_json FROM validation_results WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        run["validation"] = (
            {"validator_id": v["validator_id"], "validator_version": v["validator_version"],
             "passed": bool(v["passed"]), "checks": json.loads(v["report_json"])}
            if v else None
        )
        runs.append(run)
    out = {k: task[k] for k in task.keys() if k not in ("spec_json", "fencing_counter")}
    out["spec"] = json.loads(task["spec_json"])
    out["runs"] = runs
    return out


def supply(conn) -> dict:
    """What online computers can run right now, aggregated (no identities).
    Lets publishers pick a model someone can actually execute."""
    counts: dict[tuple[str, str], int] = {}
    for d in conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL AND ready=1 AND last_seen_at >= ?",
                          (config.now() - config.DEVICE_READY_WINDOW_S,)):
        for adapter, info in json.loads(d["capabilities_json"]).get("adapters", {}).items():
            if info.get("usage") != "known":
                continue
            for model in info.get("models") or []:
                counts[(adapter, model)] = counts.get((adapter, model), 0) + 1
    return {"options": [{"adapter": a, "model": m, "devices": n}
                        for (a, m), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]}


# --------------------------------------------------------------------------- runs (device side)

def sweep_expired(conn) -> int:
    """Lazily expire runs whose lease lapsed. The task becomes 'stalled' rather
    than auto-reopened, because the old run may still be spending at the provider."""
    now = config.now()
    stale = conn.execute(
        f"SELECT id, task_id FROM runs WHERE state IN {_ACTIVE_SQL} AND lease_until < ?", (now,)
    ).fetchall()
    if not stale:
        return 0
    n = 0
    with tx(conn):
        for r in stale:
            cur = conn.execute(
                f"UPDATE runs SET state='expired', finished_at=? WHERE id=? AND state IN {_ACTIVE_SQL} AND lease_until < ?",
                (now, r["id"], now),
            )
            if cur.rowcount != 1:
                continue
            conn.execute(
                "UPDATE tasks SET status='stalled', active_run_id=NULL, updated_at=? WHERE id=? AND active_run_id=?",
                (now, r["task_id"], r["id"]),
            )
            _event(conn, "system", "run", r["id"], "lease_expired", task_id=r["task_id"])
            n += 1
    return n


def next_run(conn, device: Device) -> dict | None:
    sweep_expired(conn)
    row = conn.execute(
        f"""SELECT r.id, r.task_id, r.fencing_token, r.state, r.lease_until, r.stop_requested, t.spec_json, t.spec_hash
            FROM runs r JOIN tasks t ON t.id = r.task_id
            WHERE r.device_id=? AND r.state IN {_ACTIVE_SQL}""",
        (device.id,),
    ).fetchone()
    if row is None:
        return None
    spec = json.loads(row["spec_json"])
    return {
        "run_id": row["id"],
        "task_id": row["task_id"],
        "fencing_token": row["fencing_token"],
        "state": row["state"],
        "lease_until": row["lease_until"],
        "stop_requested": bool(row["stop_requested"]),
        "spec": spec,
        "spec_hash": row["spec_hash"],
        "rendered_prompt": render_prompt(spec),
    }


def _load_run_for_device(conn, device: Device, run_id: str) -> sqlite3.Row:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise errors.not_found("Run")
    if run["device_id"] != device.id:
        raise errors.forbidden("Run belongs to another device")
    return run


def start_run(conn, device: Device, run_id: str, fencing_token: int) -> dict:
    """The device must win this transition before making any model call."""
    sweep_expired(conn)
    now = config.now()
    with tx(conn):
        run = _load_run_for_device(conn, device, run_id)
        if run["state"] == "running" and run["fencing_token"] == fencing_token:
            return {"run_id": run_id, "state": "running", "lease_until": run["lease_until"]}  # idempotent retry
        lease = min(now + config.RUN_LEASE_S, run["lease_hard_limit"])
        _changed(
            conn.execute(
                """UPDATE runs SET state='running', started_at=?, lease_until=?
                   WHERE id=? AND fencing_token=? AND state='assigned' AND lease_until >= ?""",
                (now, lease, run_id, fencing_token, now),
            ),
            errors.lease_expired(),
        )
        conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, device.id))
        _event(conn, device.id, "run", run_id, "started", fencing_token=fencing_token)
    return {"run_id": run_id, "state": "running", "lease_until": lease}


def heartbeat_run(conn, device: Device, run_id: str, fencing_token: int, phase: str | None) -> dict:
    sweep_expired(conn)
    now = config.now()
    if phase not in (None, "running", "uploading", "validating"):
        raise errors.invalid_input("phase must be running, uploading or validating")
    with tx(conn):
        run = _load_run_for_device(conn, device, run_id)
        lease = min(now + config.RUN_LEASE_S, run["lease_hard_limit"])
        _changed(
            conn.execute(
                """UPDATE runs SET lease_until=?, state=COALESCE(?, state)
                   WHERE id=? AND fencing_token=? AND state IN ('running','uploading','validating') AND lease_until >= ?""",
                (lease, phase, run_id, fencing_token, now),
            ),
            errors.lease_expired(),
        )
        conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, device.id))
    return {"run_id": run_id, "lease_until": lease, "stop_requested": bool(run["stop_requested"])}


def _record_late_receipt(conn, run_id: str, fencing_token: int, receipt: dict, reason: str) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO late_receipts(run_id, fencing_token, receipt_json, reason, received_at) VALUES (?,?,?,?,?)",
            (run_id, fencing_token, canonical_json(receipt), reason, config.now()),
        )
        _event(conn, "system", "run", run_id, "late_receipt", reason=reason)


def finish_run(conn, device: Device, run_id: str, fencing_token: int, receipt: dict) -> dict:
    """Validate, then settle exactly once. A repeated identical receipt returns
    the original result; a different receipt for a settled run is a conflict."""
    receipt_hash = sha256_hex(canonical_json(receipt))
    sweep_expired(conn)

    run = _load_run_for_device(conn, device, run_id)
    if run["receipt_hash"] is not None:
        prior = conn.execute(
            "SELECT response_json FROM idempotency WHERE scope='finish' AND principal=?", (run_id,)
        ).fetchone()
        if run["receipt_hash"] == receipt_hash and prior is not None:
            return json.loads(prior["response_json"])
        raise errors.idempotency_conflict()
    if run["state"] not in ACTIVE_RUN_STATES or run["fencing_token"] != fencing_token:
        reason = "stale_fencing_token" if run["fencing_token"] != fencing_token else f"run_{run['state']}"
        _record_late_receipt(conn, run_id, fencing_token, receipt, reason)
        raise errors.lease_expired(f"Receipt not accepted ({reason}); stored as late evidence")

    task = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
    spec = json.loads(task["spec_json"])
    if receipt.get("spec_hash") != task["spec_hash"]:
        raise errors.RelayError("ARTIFACT_INVALID", "Receipt spec_hash does not match the task", 422)

    usage = receipt["usage"]
    known = usage["status"] == "known"
    in_tok = int(usage.get("input_tokens") or 0) if known else None
    out_tok = int(usage.get("output_tokens") or 0) if known else None
    fee = compute_fee_milli(task["budget_milli"], in_tok, out_tok) if known else 0

    output = receipt.get("output_text") or ""
    executed_ok = receipt["status"] == "succeeded"
    check = run_validator(spec["validator_id"], spec["validator_version"], spec["validator_params"], output) if executed_ok else None
    outcome = "succeeded" if executed_ok and check.passed else "failed"
    now = config.now()

    with tx(conn):
        _changed(
            conn.execute(
                f"""UPDATE runs SET state='settled', outcome=?, receipt_hash=?, output_text=?, error_code=?,
                       usage_status=?, input_tokens=?, output_tokens=?, fee_milli=?, finished_at=?
                    WHERE id=? AND fencing_token=? AND state IN {_ACTIVE_SQL} AND receipt_hash IS NULL""",
                (outcome, receipt_hash, output, receipt.get("error_code"), usage["status"], in_tok, out_tok, fee, now,
                 run_id, fencing_token),
            ),
            errors.lease_expired(),
        )
        conn.execute(
            "INSERT INTO settlements(run_id, task_id, fee_milli, created_at) VALUES (?,?,?,?)",
            (run_id, task["id"], fee, now),
        )
        for item in usage.get("items") or []:
            conn.execute(
                "INSERT INTO usage_items(run_id, provider, unit, quantity, source, known) VALUES (?,?,?,?,?,?)",
                (run_id, item["provider"], item["unit"], int(item["quantity"]), item.get("source", "adapter"), 1 if known else 0),
            )
        if check is not None:
            conn.execute(
                """INSERT INTO validation_results(run_id, validator_id, validator_version, passed, report_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, spec["validator_id"], spec["validator_version"], 1 if check.passed else 0,
                 canonical_json(check.checks), now),
            )
        # Release the whole reservation, then move the actual fee publisher -> worker.
        _changed(
            conn.execute(
                "UPDATE accounts SET reserved_milli = reserved_milli - ?, balance_milli = balance_milli - ? WHERE id=?",
                (task["budget_milli"], fee, task["publisher_id"]),
            ),
            errors.invalid_state("publisher account missing"),
        )
        if fee:
            conn.execute("UPDATE accounts SET balance_milli = balance_milli + ? WHERE id=?", (fee, run["worker_id"]))
            conn.execute(
                "INSERT INTO ledger(task_id, run_id, account_id, amount_milli, kind, created_at) VALUES (?,?,?,?, 'settle_debit', ?)",
                (task["id"], run_id, task["publisher_id"], -fee, now),
            )
            conn.execute(
                "INSERT INTO ledger(task_id, run_id, account_id, amount_milli, kind, created_at) VALUES (?,?,?,?, 'settle_credit', ?)",
                (task["id"], run_id, run["worker_id"], fee, now),
            )
        _changed(
            conn.execute(
                "UPDATE tasks SET status=?, active_run_id=NULL, updated_at=? WHERE id=? AND status='claimed' AND active_run_id=?",
                ("done" if outcome == "succeeded" else "failed", now, task["id"], run_id),
            ),
            errors.invalid_state("task is not claimed by this run"),
        )
        _event(conn, device.id, "run", run_id, "settled", outcome=outcome, fee_milli=fee, usage_status=usage["status"])
        resp = {
            "run_id": run_id,
            "task_id": task["id"],
            "state": "settled",
            "outcome": outcome,
            "usage_status": usage["status"],
            "fee_milli": fee,
            "validation": {"passed": check.passed, "checks": check.checks} if check else None,
        }
        conn.execute(
            "INSERT INTO idempotency(scope, principal, key, request_hash, response_json, created_at) VALUES ('finish',?,?,?,?,?)",
            (run_id, run_id, receipt_hash, canonical_json(resp), now),
        )
    return resp


def stop_run(conn, *, account: Account | None = None, device: Device | None = None, run_id: str) -> dict:
    """Requests a stop. Usage already incurred is still settled under the task's rules."""
    with tx(conn):
        run = conn.execute(
            "SELECT r.*, t.publisher_id FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise errors.not_found("Run")
        allowed = (
            (device is not None and run["device_id"] == device.id)
            or (account is not None and account.id in (run["publisher_id"], run["worker_id"]))
        )
        if not allowed:
            raise errors.forbidden("Only the publisher, the worker or its device can stop this run")
        _changed(
            conn.execute(f"UPDATE runs SET stop_requested=1 WHERE id=? AND state IN {_ACTIVE_SQL}", (run_id,)),
            errors.invalid_state("Run is not active"),
        )
        actor = device.id if device else account.id
        _event(conn, actor, "run", run_id, "stop_requested")
    return {"run_id": run_id, "stop_requested": True}


# --------------------------------------------------------------------------- accounts, profiles, community

def me(conn, account: Account) -> dict:
    sweep_expired(conn)
    acct = dict(conn.execute(
        "SELECT id, handle, balance_milli, reserved_milli, credit_milli FROM accounts WHERE id=?", (account.id,)
    ).fetchone())
    acct["available_milli"] = acct["balance_milli"] - acct["reserved_milli"] + acct["credit_milli"]
    acct["can_publish"] = acct["balance_milli"] >= 0
    devices = []
    for d in conn.execute(
        "SELECT id, name, client_version, capabilities_json, ready, last_seen_at, revoked_at FROM devices WHERE account_id=? ORDER BY created_at DESC",
        (account.id,),
    ):
        devices.append({
            "id": d["id"], "name": d["name"], "client_version": d["client_version"],
            "capabilities": json.loads(d["capabilities_json"]), "online": _device_ready(d),
            "last_seen_at": d["last_seen_at"], "revoked": d["revoked_at"] is not None,
        })
    working = [dict(r) for r in conn.execute(
        f"""SELECT r.id AS run_id, r.task_id, r.state, r.outcome, r.fee_milli, t.title
            FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.worker_id=? ORDER BY r.created_at DESC LIMIT 20""",
        (account.id,),
    )]
    ledger = [dict(r) for r in conn.execute(
        "SELECT task_id, run_id, amount_milli, kind, created_at FROM ledger WHERE account_id=? ORDER BY id DESC LIMIT 20",
        (account.id,),
    )]
    return {
        "account": acct,
        "devices": devices,
        "published": list_tasks(conn, None, 20, None, publisher_id=account.id)["tasks"],
        "working": working,
        "ledger": ledger,
        "profile": get_profile(conn, account.id),
    }


def get_profile(conn, account_id: str) -> dict:
    row = conn.execute(
        "SELECT display_name, bio, work_title, work_url, public, version FROM profiles WHERE account_id=?", (account_id,)
    ).fetchone()
    if row is None:
        return {"display_name": "", "bio": "", "work_title": "", "work_url": "", "public": False, "version": 0}
    out = dict(row)
    out["public"] = bool(out["public"])
    return out


def update_profile(conn, account: Account, req: dict) -> dict:
    display_name = req.get("display_name", "").strip()[:40]
    bio = req.get("bio", "").strip()
    work_title = req.get("work_title", "").strip()[:80]
    work_url = req.get("work_url", "").strip()
    public = bool(req.get("public"))
    if len(bio) > config.BIO_MAX_CHARS:
        raise errors.invalid_input(f"bio must be at most {config.BIO_MAX_CHARS} characters")
    if work_url:
        u = urlparse(work_url)
        if u.scheme != "https" or not u.netloc or len(work_url) > 300:
            raise errors.invalid_input("work_url must be an https:// URL")
    if public and not display_name:
        raise errors.invalid_input("A public profile needs a display name")
    now = config.now()
    with tx(conn):
        prev = conn.execute("SELECT version FROM profiles WHERE account_id=?", (account.id,)).fetchone()
        version = (prev["version"] if prev else 0) + 1
        conn.execute(
            """INSERT INTO profiles(account_id, display_name, bio, work_title, work_url, public, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET display_name=excluded.display_name, bio=excluded.bio,
                   work_title=excluded.work_title, work_url=excluded.work_url, public=excluded.public,
                   version=excluded.version, updated_at=excluded.updated_at""",
            (account.id, display_name, bio, work_title, work_url, 1 if public else 0, version, now),
        )
        snapshot = {"display_name": display_name, "bio": bio, "work_title": work_title, "work_url": work_url}
        conn.execute(
            "INSERT INTO profile_versions(account_id, version, public, snapshot_json, created_at) VALUES (?,?,?,?,?)",
            (account.id, version, 1 if public else 0, canonical_json(snapshot), now),
        )
        _event(conn, account.id, "profile", account.id, "updated", version=version, public=public)
    return get_profile(conn, account.id)


def community(conn, settings: Settings) -> dict:
    """Cumulative earned credit (positive settlement income only). Spending never
    lowers historical rank; initial credit is not income."""
    rows = conn.execute(
        """SELECT l.account_id, SUM(l.amount_milli) AS earned_milli, MAX(l.created_at) AS last_income_at,
                  COUNT(DISTINCT l.task_id) AS tasks,
                  p.public, p.display_name, p.bio, p.work_title, p.work_url, p.showcase_suspended
           FROM ledger l LEFT JOIN profiles p ON p.account_id = l.account_id
           WHERE l.kind='settle_credit'
           GROUP BY l.account_id
           ORDER BY earned_milli DESC, last_income_at ASC, l.account_id ASC
           LIMIT ?""",
        (config.LEADERBOARD_SIZE,),
    ).fetchall()
    board = []
    for rank, r in enumerate(rows, start=1):
        public = bool(r["public"])
        entry = {
            "rank": rank,
            "account_id": r["account_id"],
            "name": r["display_name"] if public else f"匿名成员 {sha256_hex(r['account_id'])[:6]}",
            "earned_milli": r["earned_milli"],
            "tasks": r["tasks"],
        }
        # Top-N showcase slots stay empty if the holder is not public; no fall-through to rank N+1.
        if settings.showcase_enabled and rank <= config.SHOWCASE_SIZE and public and not r["showcase_suspended"]:
            entry["showcase"] = {"bio": r["bio"], "work_title": r["work_title"], "work_url": r["work_url"]}
        board.append(entry)
    return {"leaderboard": board}


def report_account(conn, account: Account, target_account_id: str, reason: str) -> dict:
    reason = reason.strip()
    if not (5 <= len(reason) <= 500):
        raise errors.invalid_input("reason must be 5-500 characters")
    if target_account_id == account.id:
        raise errors.invalid_input("You cannot report yourself")
    with tx(conn):
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (target_account_id,)).fetchone():
            raise errors.not_found("Account")
        open_reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE reporter_id=? AND created_at > ?", (account.id, config.now() - 86400)
        ).fetchone()[0]
        if open_reports >= 10:
            raise errors.RelayError("RATE_LIMITED", "Too many reports today", 429)
        cur = conn.execute(
            "INSERT INTO reports(reporter_id, target_account_id, reason, created_at) VALUES (?,?,?,?)",
            (account.id, target_account_id, reason, config.now()),
        )
        _event(conn, account.id, "account", target_account_id, "reported", report_id=cur.lastrowid)
    return {"ok": True}


def list_reports(conn, include_resolved: bool = False) -> list[dict]:
    sql = """SELECT r.*, a.handle AS target_handle, p.display_name, p.work_url, p.showcase_suspended
             FROM reports r JOIN accounts a ON a.id=r.target_account_id
             LEFT JOIN profiles p ON p.account_id=r.target_account_id"""
    if not include_resolved:
        sql += " WHERE r.resolved_at IS NULL"
    return [dict(r) for r in conn.execute(sql + " ORDER BY r.id DESC LIMIT 200")]


def set_showcase_suspended(conn, account_id: str, suspended: bool, report_id: int | None, note: str) -> dict:
    """Pauses the showcase slot only; earned credit and rank are untouched (section 09)."""
    now = config.now()
    with tx(conn):
        conn.execute(
            """INSERT INTO profiles(account_id, showcase_suspended, updated_at) VALUES (?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET showcase_suspended=excluded.showcase_suspended""",
            (account_id, 1 if suspended else 0, now),
        )
        if report_id is not None:
            conn.execute("UPDATE reports SET resolved_at=?, resolution=? WHERE id=?",
                         (now, ("suspended: " if suspended else "kept: ") + note[:200], report_id))
        _event(conn, "admin", "profile", account_id, "showcase_suspended" if suspended else "showcase_restored", note=note[:200])
    return {"account_id": account_id, "showcase_suspended": suspended}


def stats(conn) -> dict:
    """Pilot metrics (section 14). Technical completion counts every started run."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    started = q("SELECT COUNT(*) FROM runs WHERE started_at IS NOT NULL")
    succeeded = q("SELECT COUNT(*) FROM runs WHERE outcome='succeeded'")
    settled = q("SELECT COUNT(*) FROM runs WHERE state='settled'")
    unknown = q("SELECT COUNT(*) FROM runs WHERE state='settled' AND usage_status='unknown'")
    return {
        "accounts": q("SELECT COUNT(*) FROM accounts"),
        "devices_online": q("SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL AND ready=1 AND last_seen_at >= ?",
                            config.now() - config.DEVICE_READY_WINDOW_S),
        "tasks_by_status": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")},
        "runs_started": started,
        "runs_succeeded": succeeded,
        "runs_expired": q("SELECT COUNT(*) FROM runs WHERE state='expired'"),
        "technical_completion": round(succeeded / started, 3) if started else None,
        "unknown_usage_share": round(unknown / settled, 3) if settled else None,
        "late_receipts": q("SELECT COUNT(*) FROM late_receipts"),
        "open_reports": q("SELECT COUNT(*) FROM reports WHERE resolved_at IS NULL"),
    }


# --------------------------------------------------------------------------- reconciliation

def reconcile(conn) -> dict:
    """Ledger invariants from section 08. Every list must be empty."""
    problems: dict[str, list] = {}
    total = conn.execute("SELECT COALESCE(SUM(amount_milli),0) FROM ledger").fetchone()[0]
    if total != 0:
        problems["ledger_not_zero_sum"] = [total]
    problems["balance_mismatch"] = [dict(r) for r in conn.execute(
        f"""SELECT a.id, a.balance_milli, {config.INITIAL_BALANCE_MILLI} + COALESCE(SUM(l.amount_milli),0) AS ledger_balance
            FROM accounts a LEFT JOIN ledger l ON l.account_id=a.id GROUP BY a.id
            HAVING a.balance_milli != ledger_balance"""
    )]
    problems["reserved_mismatch"] = [dict(r) for r in conn.execute(
        """SELECT a.id, a.reserved_milli,
                  COALESCE((SELECT SUM(budget_milli) FROM tasks t WHERE t.publisher_id=a.id
                            AND t.status IN ('open','claimed','stalled')),0) AS expected
           FROM accounts a WHERE a.reserved_milli != expected"""
    )]
    problems["fee_over_budget"] = [dict(r) for r in conn.execute(
        "SELECT s.run_id, s.fee_milli, t.budget_milli FROM settlements s JOIN tasks t ON t.id=s.task_id WHERE s.fee_milli > t.budget_milli"
    )]
    problems["settled_task_still_reserved"] = [dict(r) for r in conn.execute(
        "SELECT t.id, t.status FROM settlements s JOIN tasks t ON t.id=s.task_id WHERE t.status IN ('open','claimed','stalled')"
    )]
    problems["ledger_without_settlement"] = [dict(r) for r in conn.execute(
        """SELECT l.run_id, SUM(l.amount_milli) AS net, COUNT(*) AS n FROM ledger l
           WHERE l.kind IN ('settle_debit','settle_credit') GROUP BY l.run_id
           HAVING net != 0 OR n != 2 OR l.run_id NOT IN (SELECT run_id FROM settlements)"""
    )]
    problems = {k: v for k, v in problems.items() if v}
    return {"ok": not problems, "problems": problems}
