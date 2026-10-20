"""HTTP API (v2). See section 12 for the contract."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import PROTOCOL_VERSION, __version__, config, errors, oauth, service
from .config import Settings
from .db import connect, migrate
from .templates import public_templates
from .web import HardeningMiddleware

SESSION_COOKIE = "relay_session"
STATE_COOKIE = "relay_oauth"

STATIC = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- request bodies

class LoginBody(BaseModel):
    handle: str


class PairBody(BaseModel):
    code: str
    name: str = "device"
    capabilities: dict = Field(default_factory=dict)
    client_version: str = ""
    protocol_version: int = PROTOCOL_VERSION


class DeviceHeartbeatBody(BaseModel):
    ready: bool = True
    capabilities: dict | None = None
    client_version: str | None = None


class InputItem(BaseModel):
    name: str = Field(max_length=120)
    text: str = Field(max_length=100_000)


class ValidatorSpec(BaseModel):
    id: str
    params: dict = Field(default_factory=dict)


class CreateTaskBody(BaseModel):
    category: str
    title: str = Field(max_length=120)
    prompt: str | None = Field(default=None, max_length=20_000)
    inputs: list[InputItem] = Field(default_factory=list, max_length=10)
    deliverable: str | None = Field(default=None, max_length=4_000)
    deliverable_schema: dict | None = None
    adapter: str = Field(max_length=32)
    model: str = Field(max_length=64)
    required_tools: list[str] = Field(default_factory=list)
    budget_milli: int
    max_runtime_s: int | None = None
    validator: ValidatorSpec | None = None
    public_consent: bool = False


class ClaimBody(BaseModel):
    device_id: str


class ReopenBody(BaseModel):
    acknowledge_duplicate_cost: bool = False


class FencedBody(BaseModel):
    fencing_token: int


class RunHeartbeatBody(FencedBody):
    phase: Literal["running", "uploading", "validating"] | None = None


class UsageItem(BaseModel):
    provider: str = Field(max_length=32)
    unit: str = Field(max_length=32)
    quantity: int = Field(ge=0)
    source: str = Field(default="adapter", max_length=64)


class Usage(BaseModel):
    status: Literal["known", "unknown"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    items: list[UsageItem] = Field(default_factory=list, max_length=20)


class Receipt(BaseModel):
    status: Literal["succeeded", "failed"]
    spec_hash: str
    output_text: str = Field(default="", max_length=200_000)
    error_code: str | None = Field(default=None, max_length=64)
    usage: Usage
    adapter: dict = Field(default_factory=dict)


class FinishBody(FencedBody):
    receipt: Receipt


class ReportBody(BaseModel):
    target_account_id: str = Field(max_length=64)
    reason: str = Field(max_length=500)


class InviteBody(BaseModel):
    n: int = 1
    max_uses: int = 1
    note: str = ""
    ttl_days: int | None = 30


class SuspendBody(BaseModel):
    suspended: bool
    report_id: int | None = None
    note: str = ""


class ProfileBody(BaseModel):
    display_name: str = ""
    bio: str = ""
    work_title: str = ""
    work_url: str = ""
    public: bool = False


# --------------------------------------------------------------------------- app

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    boot = connect(settings.db_path)
    migrate(boot)
    boot.close()

    app = FastAPI(title="Agent Relay", version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(HardeningMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(errors.RelayError)
    async def _relay_error(_: Request, exc: errors.RelayError):
        return JSONResponse(status_code=exc.status, content={"error": exc.to_dict()})

    def db() -> Iterator:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def bearer(authorization: str | None = Header(default=None)) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    def origin_of(request: Request) -> str:
        return (settings.public_url or str(request.base_url)).rstrip("/")

    def user(request: Request, conn=Depends(db), token: str | None = Depends(bearer)) -> service.Account:
        if token is None:
            token = request.cookies.get(SESSION_COOKIE)
            # Cookie-authenticated writes must come from our own pages (CSRF).
            if token and request.method not in ("GET", "HEAD"):
                src = request.headers.get("origin") or request.headers.get("referer") or ""
                if not (src == origin_of(request) or src.startswith(origin_of(request) + "/")):
                    raise errors.forbidden("Cross-origin request refused")
        return service.account_from_token(conn, token)

    def admin(token: str | None = Depends(bearer)) -> None:
        if not settings.admin_token or not token or not hmac.compare_digest(token, settings.admin_token):
            raise errors.forbidden("Admin token required")

    def set_session(resp, token: str) -> None:
        resp.set_cookie(SESSION_COOKIE, token, max_age=config.SESSION_TTL_S, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path="/")

    def device(conn=Depends(db), token: str | None = Depends(bearer)) -> service.Device:
        return service.device_from_token(conn, token)

    # -- meta / static
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz(conn=Depends(db)):
        conn.execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.get("/v2/meta")
    def meta():
        return {
            "version": __version__, "protocol_version": PROTOCOL_VERSION, "templates": public_templates(),
            "login": {"github": settings.github_enabled, "dev": settings.allow_dev_login, "invite_only": not settings.open_signup},
            "test_adapters": settings.allow_test_adapters,
        }

    # -- identity
    @app.post("/v2/auth/dev-login")
    def dev_login(body: LoginBody, conn=Depends(db)):
        res = service.dev_login(conn, settings, body.handle)
        resp = JSONResponse(res)
        set_session(resp, res["token"])
        return resp

    @app.get("/v2/auth/github/start")
    def github_start(request: Request, invite: str = ""):
        if not settings.github_enabled:
            raise errors.forbidden("GitHub login is not configured")
        state = secrets.token_urlsafe(24)
        resp = RedirectResponse(oauth.authorize_url(settings.github_client_id, origin_of(request) + "/v2/auth/github/callback", state), 302)
        # State + invite ride in a short-lived HttpOnly cookie; GitHub echoes the state back.
        resp.set_cookie(STATE_COOKIE, f"{state}.{invite.strip()[:32]}", max_age=600, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path="/v2/auth/github")
        return resp

    @app.get("/v2/auth/github/callback")
    def github_callback(request: Request, code: str = "", state: str = "", conn=Depends(db)):
        saved = request.cookies.get(STATE_COOKIE, "")
        saved_state, _, invite = saved.partition(".")
        def fail(code_: str):
            r = RedirectResponse(f"/?login_error={code_}", 302)
            r.delete_cookie(STATE_COOKIE, path="/v2/auth/github")
            return r
        if not code or not saved_state or not hmac.compare_digest(saved_state, state):
            return fail("STATE_MISMATCH")
        try:
            gh_id, login = oauth.fetch_github_user(settings.github_client_id, settings.github_client_secret, code,
                                                   origin_of(request) + "/v2/auth/github/callback")
            res = service.github_login(conn, settings, gh_id, login, invite)
        except errors.RelayError as e:
            return fail(e.code)
        resp = RedirectResponse("/", 302)
        resp.delete_cookie(STATE_COOKIE, path="/v2/auth/github")
        set_session(resp, res["token"])
        return resp

    @app.post("/v2/auth/logout")
    def logout(request: Request, conn=Depends(db), token: str | None = Depends(bearer)):
        service.logout(conn, token or request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/v2/me")
    def get_me(acct=Depends(user), conn=Depends(db)):
        return service.me(conn, acct)

    @app.put("/v2/me/profile")
    def put_profile(body: ProfileBody, acct=Depends(user), conn=Depends(db)):
        return service.update_profile(conn, acct, body.model_dump())

    # -- devices
    @app.post("/v2/devices/pair-codes")
    def pair_code(acct=Depends(user), conn=Depends(db)):
        return service.create_pair_code(conn, acct)

    @app.post("/v2/devices/pair")
    def pair(body: PairBody, conn=Depends(db)):
        return service.pair_device(conn, body.code, body.name, body.capabilities, body.client_version, body.protocol_version)

    @app.post("/v2/devices/heartbeat")
    def dev_heartbeat(body: DeviceHeartbeatBody, dev=Depends(device), conn=Depends(db)):
        return service.device_heartbeat(conn, dev, body.ready, body.capabilities, body.client_version)

    @app.post("/v2/devices/{device_id}/revoke")
    def revoke(device_id: str, acct=Depends(user), conn=Depends(db)):
        return service.revoke_device(conn, acct, device_id)

    # -- tasks
    @app.post("/v2/tasks")
    def create_task(body: CreateTaskBody, acct=Depends(user), conn=Depends(db),
                    idempotency_key: str | None = Header(default=None)):
        return service.create_task(conn, settings, acct, body.model_dump(), idempotency_key)

    @app.get("/v2/tasks")
    def list_tasks(status: str | None = "open", limit: int = 20, before: str | None = None,
                   acct=Depends(user), conn=Depends(db)):
        return service.list_tasks(conn, status or None, limit, before)

    @app.get("/v2/tasks/{task_id}")
    def get_task(task_id: str, acct=Depends(user), conn=Depends(db)):
        return service.get_task(conn, task_id)

    @app.post("/v2/tasks/{task_id}/claim")
    def claim(task_id: str, body: ClaimBody, acct=Depends(user), conn=Depends(db),
              idempotency_key: str | None = Header(default=None)):
        return service.claim_task(conn, settings, acct, task_id, body.device_id, idempotency_key)

    @app.post("/v2/tasks/{task_id}/cancel")
    def cancel(task_id: str, acct=Depends(user), conn=Depends(db)):
        return service.cancel_task(conn, acct, task_id)

    @app.post("/v2/tasks/{task_id}/reopen")
    def reopen(task_id: str, body: ReopenBody, acct=Depends(user), conn=Depends(db)):
        return service.reopen_task(conn, acct, task_id, body.acknowledge_duplicate_cost)

    # -- runs (device)
    @app.get("/v2/runs/next")
    def next_run(dev=Depends(device), conn=Depends(db)):
        return {"run": service.next_run(conn, dev)}

    @app.post("/v2/runs/{run_id}/start")
    def start(run_id: str, body: FencedBody, dev=Depends(device), conn=Depends(db)):
        return service.start_run(conn, dev, run_id, body.fencing_token)

    @app.post("/v2/runs/{run_id}/heartbeat")
    def run_heartbeat(run_id: str, body: RunHeartbeatBody, dev=Depends(device), conn=Depends(db)):
        return service.heartbeat_run(conn, dev, run_id, body.fencing_token, body.phase)

    @app.post("/v2/runs/{run_id}/finish")
    def finish(run_id: str, body: FinishBody, dev=Depends(device), conn=Depends(db)):
        return service.finish_run(conn, dev, run_id, body.fencing_token, body.receipt.model_dump())

    @app.post("/v2/runs/{run_id}/stop")
    def stop(run_id: str, conn=Depends(db), token: str | None = Depends(bearer)):
        if token and token.startswith("d_"):
            return service.stop_run(conn, device=service.device_from_token(conn, token), run_id=run_id)
        return service.stop_run(conn, account=service.account_from_token(conn, token), run_id=run_id)

    # -- community / admin
    @app.get("/v2/community")
    def community(conn=Depends(db)):
        return service.community(conn, settings)

    @app.post("/v2/reports")
    def report(body: ReportBody, acct=Depends(user), conn=Depends(db)):
        return service.report_account(conn, acct, body.target_account_id, body.reason)

    @app.get("/v2/admin/reconcile", dependencies=[Depends(admin)])
    def reconcile(conn=Depends(db)):
        return service.reconcile(conn)

    @app.get("/v2/admin/stats", dependencies=[Depends(admin)])
    def stats(conn=Depends(db)):
        return service.stats(conn)

    @app.post("/v2/admin/invites", dependencies=[Depends(admin)])
    def invites(body: InviteBody, conn=Depends(db)):
        return {"codes": service.create_invites(conn, body.n, body.max_uses, body.note, body.ttl_days)}

    @app.get("/v2/admin/reports", dependencies=[Depends(admin)])
    def reports(include_resolved: bool = False, conn=Depends(db)):
        return {"reports": service.list_reports(conn, include_resolved)}

    @app.post("/v2/admin/accounts/{account_id}/showcase", dependencies=[Depends(admin)])
    def suspend(account_id: str, body: SuspendBody, conn=Depends(db)):
        return service.set_showcase_suspended(conn, account_id, body.suspended, body.report_id, body.note)

    return app
