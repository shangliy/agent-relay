"""Structured error codes (section 12)."""

from __future__ import annotations


class RelayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint

    def to_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        return body


def auth_required(msg: str = "Authentication required") -> RelayError:
    return RelayError("AUTH_REQUIRED", msg, 401, "Sign in again or re-pair the device.")


def device_revoked() -> RelayError:
    return RelayError("DEVICE_REVOKED", "This device key has been revoked", 401, "Generate a new pair code on the web.")


def forbidden(msg: str = "Not allowed") -> RelayError:
    return RelayError("FORBIDDEN", msg, 403)


def not_found(what: str) -> RelayError:
    return RelayError("NOT_FOUND", f"{what} not found", 404)


def invalid_input(msg: str) -> RelayError:
    return RelayError("INVALID_INPUT", msg, 422)


def invalid_state(msg: str) -> RelayError:
    return RelayError("INVALID_STATE", msg, 409)


def capability_mismatch(msg: str) -> RelayError:
    return RelayError("CAPABILITY_MISMATCH", msg, 409, "Pick a device whose adapter/model/tools match the task.")


def insufficient_credit(msg: str) -> RelayError:
    return RelayError("INSUFFICIENT_CREDIT", msg, 409, "Complete tasks for others to earn credit, or lower the budget.")


def task_already_claimed() -> RelayError:
    return RelayError("TASK_ALREADY_CLAIMED", "Task is no longer open", 409)


def lease_expired(msg: str = "Run lease expired or fencing token is stale") -> RelayError:
    return RelayError("LEASE_EXPIRED", msg, 409, "Stop this run locally; do not retry the model call.")


def idempotency_conflict() -> RelayError:
    return RelayError("IDEMPOTENCY_CONFLICT", "Same idempotency key used with different content", 409)


def device_not_ready() -> RelayError:
    return RelayError("DEVICE_NOT_READY", "Device has not reported ready recently", 409, "Start the local worker and wait for it to come online.")


def disabled(what: str) -> RelayError:
    return RelayError("DISABLED", f"{what} is temporarily disabled", 503)
