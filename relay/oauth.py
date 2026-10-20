"""GitHub OAuth (web application flow). Only the numeric id and login are kept;
the GitHub access token is used once and discarded."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import errors

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    # No scopes: public profile (id, login) only.
    q = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state, "allow_signup": "true"})
    return f"{AUTHORIZE_URL}?{q}"


def _post_json(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                               "User-Agent": "agent-relay"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_github_user(client_id: str, client_secret: str, code: str, redirect_uri: str) -> tuple[int, str]:
    try:
        tok = _post_json(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret, "code": code,
                                     "redirect_uri": redirect_uri})
        access = tok.get("access_token")
        if not access:
            raise errors.RelayError("OAUTH_FAILED", f"GitHub did not return a token ({tok.get('error', 'unknown')})", 400)
        user = _get_json(USER_URL, access)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise errors.RelayError("OAUTH_FAILED", "Could not reach GitHub", 502) from e
    if not isinstance(user.get("id"), int) or not user.get("login"):
        raise errors.RelayError("OAUTH_FAILED", "Unexpected GitHub user response", 502)
    return user["id"], str(user["login"])
