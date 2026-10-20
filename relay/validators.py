"""Fixed, versioned deterministic checks (section 11).

A pass here means "format passed" — never "content is correct or useful".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import jsonschema


@dataclass
class CheckResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": ok, "detail": detail})
        if not ok:
            self.passed = False


_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")


def count_words(text: str) -> int:
    """CJK characters count individually; Latin runs count as words."""
    return len(_CJK.findall(text)) + len(_WORD.findall(_CJK.sub(" ", text)))


def _nonempty(text: str, params: dict, r: CheckResult) -> None:
    r.add("nonempty", bool(text.strip()), "" if text.strip() else "output is empty")


def _json(text: str, params: dict, r: CheckResult) -> object | None:
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError as e:
        r.add("json_parse", False, str(e)[:200])
        return None
    r.add("json_parse", True)
    return obj


def _json_schema(text: str, params: dict, r: CheckResult) -> None:
    obj = _json(text, params, r)
    if obj is None:
        return
    try:
        jsonschema.validate(obj, params["schema"])
        r.add("json_schema", True)
    except jsonschema.ValidationError as e:
        r.add("json_schema", False, f"{'/'.join(map(str, e.absolute_path))}: {e.message}"[:300])


def _min_words(text: str, params: dict, r: CheckResult) -> None:
    n = count_words(text)
    need = int(params.get("min", 1))
    r.add("min_words", n >= need, f"{n} words, need {need}")


def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    return m.group(1) if m else t


VALIDATORS = {
    "nonempty": ("1", _nonempty),
    "json": ("1", _json),
    "json_schema": ("1", _json_schema),
    "min_words": ("1", _min_words),
}


def check_params(validator_id: str, params: dict) -> None:
    if validator_id not in VALIDATORS:
        raise ValueError(f"unknown validator {validator_id!r}")
    if validator_id == "json_schema":
        schema = params.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("json_schema validator needs params.schema")
        jsonschema.Draft202012Validator.check_schema(schema)
    if validator_id == "min_words" and not (isinstance(params.get("min"), int) and params["min"] > 0):
        raise ValueError("min_words validator needs positive integer params.min")


def run_validator(validator_id: str, version: str, params: dict, text: str) -> CheckResult:
    current_version, fn = VALIDATORS[validator_id]
    if version != current_version:
        # In-flight tasks must keep their pinned checker; refuse rather than silently upgrade.
        raise ValueError(f"validator {validator_id} v{version} not available (have v{current_version})")
    r = CheckResult(passed=True)
    _nonempty(text, {}, r)
    if r.passed and validator_id != "nonempty":
        fn(text, params, r)
    return r
