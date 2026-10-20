"""Public-pilot features: GitHub login + invites, cookie sessions, CSRF, rate limits, reports."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from relay import config, oauth
from relay.api import create_app
from relay.config import Settings

ADMIN = {"Authorization": "Bearer admin-secret"}
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def gh_settings(tmp_path):
    return Settings(db_path=str(tmp_path / "p.db"), github_client_id="cid", github_client_secret="secret",
                    admin_token="admin-secret", rate_limits=False)


@pytest.fixture
def gh(gh_settings, clock, monkeypatch):
    users = {"good": (101, "Octo"), "other": (202, "octo")}
    monkeypatch.setattr(oauth, "fetch_github_user", lambda cid, sec, code, redirect: users[code])
    return TestClient(create_app(gh_settings), follow_redirects=False)


def github_login(c: TestClient, code: str, invite: str = "") -> str:
    r = c.get("/v2/auth/github/start", params={"invite": invite})
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/v2/auth/github/callback", params={"code": code, "state": state})
    assert r.status_code == 302
    return r.headers["location"]


def invite(c: TestClient, max_uses=1) -> str:
    return c.post("/v2/admin/invites", headers=ADMIN, json={"n": 1, "max_uses": max_uses}).json()["codes"][0]


def test_dev_login_off_by_default(gh):
    assert gh.post("/v2/auth/dev-login", json={"handle": "x1"}).status_code == 403
    assert gh.get("/v2/meta").json()["login"] == {"github": True, "dev": False, "invite_only": True}


def test_invite_required_for_new_accounts(gh):
    assert github_login(gh, "good") == "/?login_error=INVITE_REQUIRED"
    assert gh.get("/v2/me").status_code == 401

    code = invite(gh)
    assert github_login(gh, "good", code) == "/"
    me = gh.get("/v2/me").json()
    assert me["account"]["handle"] == "octo"

    # Invite is single use; returning users need none.
    gh.cookies.clear()
    assert github_login(gh, "other", code) == "/?login_error=INVITE_REQUIRED"
    gh.cookies.clear()
    assert github_login(gh, "good") == "/"
    assert gh.get("/v2/me").status_code == 200


def test_handle_collision_gets_suffix(gh):
    code = invite(gh, max_uses=2)
    github_login(gh, "good", code)
    gh.cookies.clear()
    github_login(gh, "other", code)  # login "octo" again, different GitHub id
    assert gh.get("/v2/me").json()["account"]["handle"].startswith("octo-")


def test_state_mismatch_rejected(gh):
    gh.get("/v2/auth/github/start")
    r = gh.get("/v2/auth/github/callback", params={"code": "good", "state": "forged"})
    assert r.headers["location"] == "/?login_error=STATE_MISMATCH"


def test_cookie_writes_need_same_origin(gh):
    github_login(gh, "good", invite(gh))
    assert gh.post("/v2/devices/pair-codes").status_code == 403
    assert gh.post("/v2/devices/pair-codes", headers={"Origin": "https://evil.example"}).status_code == 403
    assert gh.post("/v2/devices/pair-codes", headers=ORIGIN).status_code == 200


def test_session_expiry_and_logout(gh, clock):
    github_login(gh, "good", invite(gh))
    assert gh.get("/v2/me").status_code == 200
    clock.advance(config.SESSION_TTL_S + 1)
    assert gh.get("/v2/me").status_code == 401
    gh.cookies.clear()
    github_login(gh, "good")
    assert gh.post("/v2/auth/logout", headers=ORIGIN).status_code == 200
    assert gh.get("/v2/me").status_code == 401


def test_admin_endpoints_need_token(gh):
    assert gh.get("/v2/admin/stats").status_code == 403
    assert gh.get("/v2/admin/stats", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert gh.get("/v2/admin/stats", headers=ADMIN).json()["accounts"] == 0


def test_rate_limit_and_headers(tmp_path, clock):
    c = TestClient(create_app(Settings(db_path=str(tmp_path / "r.db"), allow_dev_login=True)))
    codes = [c.post("/v2/auth/dev-login", json={"handle": "spam"}).status_code for _ in range(25)]
    assert codes.count(429) == 5
    r = c.get("/v2/meta")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_report_and_suspend_showcase(relay):
    from tests.test_community import _earn

    w = relay.login("worker")
    _earn(relay, relay.login("pub"), w, out_tokens=1000)
    relay.c.put("/v2/me/profile", headers=w, json={"display_name": "W", "work_url": "https://w.example", "public": True})
    entry = relay.c.get("/v2/community").json()["leaderboard"][0]
    assert entry["showcase"]

    reporter = relay.login("reporter")
    assert relay.c.post("/v2/reports", headers=reporter, json={"target_account_id": entry["account_id"], "reason": "x"}).status_code == 422
    assert relay.c.post("/v2/reports", headers=reporter,
                        json={"target_account_id": entry["account_id"], "reason": "spam link"}).status_code == 200
    admin = {"Authorization": "Bearer admin-secret"}
    reports = relay.c.get("/v2/admin/reports", headers=admin).json()["reports"]
    assert len(reports) == 1
    relay.c.post(f"/v2/admin/accounts/{entry['account_id']}/showcase", headers=admin,
                 json={"suspended": True, "report_id": reports[0]["id"], "note": "spam"})
    entry2 = relay.c.get("/v2/community").json()["leaderboard"][0]
    assert "showcase" not in entry2 and entry2["earned_milli"] == entry["earned_milli"]  # credit untouched
    assert relay.c.get("/v2/admin/reports", headers=admin).json()["reports"] == []
    # Editing the profile does not lift the suspension.
    relay.c.put("/v2/me/profile", headers=w, json={"display_name": "W2", "work_url": "https://w.example", "public": True})
    assert "showcase" not in relay.c.get("/v2/community").json()["leaderboard"][0]


def test_stats(relay):
    from tests.test_community import _earn

    _earn(relay, relay.login("pub"), relay.login("worker"), out_tokens=10)
    s = relay.c.get("/v2/admin/stats", headers={"Authorization": "Bearer admin-secret"}).json()
    assert s["runs_started"] == 1 and s["technical_completion"] == 1.0 and s["unknown_usage_share"] == 0.0
