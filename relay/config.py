"""Platform rules and runtime parameters.

Values mirror sections 07/08 of the design doc. They are current implementation
parameters, not service-level commitments.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

MILLI_PER_RP = 1_000

# Credit pricing (section 08): 1 milli RP per input token, 3 per output token.
INPUT_TOKEN_MILLI = 1
OUTPUT_TOKEN_MILLI = 3

INITIAL_BALANCE_MILLI = 0
CREDIT_LIMIT_MILLI = 20 * MILLI_PER_RP
MIN_BUDGET_MILLI = 1 * MILLI_PER_RP
MAX_BUDGET_MILLI = 100 * MILLI_PER_RP

# Device liveness / leases (section 07).
DEVICE_READY_WINDOW_S = 90
ASSIGN_LEASE_S = 120          # time for the device to pick up and start a run
RUN_LEASE_S = 90              # each heartbeat extends the lease by this much
LEASE_GRACE_S = 120           # hard limit = start + max_runtime + grace
DEFAULT_MAX_RUNTIME_S = 600
MAX_MAX_RUNTIME_S = 3600
PAIR_CODE_TTL_S = 600

# Community (section 09).
LEADERBOARD_SIZE = 10
SHOWCASE_SIZE = 3
BIO_MAX_CHARS = 180

# Task graph limits (section 06) — reserved for the orchestrator phase.
MAX_TASK_DEPTH = 2
MAX_CHILD_TASKS = 5


def _env(name: str, default: str = "") -> Callable[[], str]:
    return lambda: os.environ.get(name, default)


def _flag(name: str, default: str) -> Callable[[], bool]:
    return lambda: os.environ.get(name, default) == "1"


@dataclass
class Settings:
    """Read from the environment when constructed (not at import)."""
    db_path: str = field(default_factory=_env("RELAY_DB", "agent_relay.db"))
    # Public origin, e.g. https://relay.example.org. Enables Secure cookies and
    # is the origin browser writes must come from.
    public_url: str = field(default_factory=_env("RELAY_PUBLIC_URL"))
    # Dev login lets anyone mint an account by handle. Local use only.
    allow_dev_login: bool = field(default_factory=_flag("RELAY_DEV_LOGIN", "0"))
    github_client_id: str = field(default_factory=_env("GITHUB_CLIENT_ID"))
    github_client_secret: str = field(default_factory=_env("GITHUB_CLIENT_SECRET"))
    # Invite-only pilot: new accounts need an invite code unless this is on.
    open_signup: bool = field(default_factory=_flag("RELAY_OPEN_SIGNUP", "0"))
    admin_token: str = field(default_factory=_env("RELAY_ADMIN_TOKEN"))
    # Trust Fly-Client-IP / X-Forwarded-For for rate limiting (behind a proxy only).
    trust_proxy: bool = field(default_factory=_flag("RELAY_TRUST_PROXY", "0"))
    rate_limits: bool = field(default_factory=_flag("RELAY_RATE_LIMITS", "1"))
    # Kill switches (section 14 release checklist).
    allow_new_tasks: bool = field(default_factory=_flag("RELAY_ALLOW_NEW_TASKS", "1"))
    allow_new_claims: bool = field(default_factory=_flag("RELAY_ALLOW_NEW_CLAIMS", "1"))
    showcase_enabled: bool = field(default_factory=_flag("RELAY_SHOWCASE", "1"))
    # The model-free echo adapter must never earn real credit.
    allow_test_adapters: bool = field(default_factory=_flag("RELAY_TEST_ADAPTERS", "0"))

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)


SESSION_TTL_S = 30 * 24 * 3600


def now() -> int:
    """Wall clock in integer seconds. Tests monkeypatch this."""
    return int(time.time())
