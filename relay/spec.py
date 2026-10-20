"""Task spec v2: canonical JSON manifest and hashing (section 06).

Canonical form: UTF-8, keys sorted, no insignificant whitespace, non-ASCII kept
as-is. The hash proves byte identity of the task package only — not that the
host executed it faithfully.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = 2


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def spec_hash(spec: dict) -> str:
    return sha256_hex(canonical_json(spec))


def build_spec(
    *,
    category: str,
    template_version: str,
    title: str,
    prompt: str,
    inputs: list[dict],
    deliverable: str,
    deliverable_schema: dict | None,
    adapter: str,
    model: str,
    required_tools: list[str],
    budget_milli: int,
    max_runtime_s: int,
    validator_id: str,
    validator_version: str,
    validator_params: dict,
) -> dict:
    """Every field that affects execution or billing goes into the hash."""
    return {
        "schema_version": SCHEMA_VERSION,
        "template_version": template_version,
        "category": category,
        "title": title,
        "prompt": prompt,
        "input_manifest": inputs,
        "deliverable": deliverable,
        "deliverable_schema": deliverable_schema,
        "public_consent": True,
        "adapter": adapter,
        "model": model,
        "required_tools": sorted(required_tools),
        "network_allowlist": [],
        "execution_profile": "no-tools" if not required_tools else "restricted-tools",
        "reward_cap_milli": budget_milli,
        "generation_cap_milli": budget_milli,
        "validation_cap_milli": 0,
        "max_runtime_s": max_runtime_s,
        "validator_id": validator_id,
        "validator_version": validator_version,
        "validator_params": validator_params,
        "failure_payment_policy": "known_usage",
        "pricing": {"input_token_milli": 1, "output_token_milli": 3, "version": "rp-v1"},
    }


def render_prompt(spec: dict) -> str:
    """The exact text an adapter sends to the model. Lives server-side so the
    worker and any verifier render identically."""
    parts = [spec["prompt"].strip()]
    for item in spec.get("input_manifest") or []:
        parts.append(f"\n\n--- 输入材料: {item['name']} ---\n{item['text']}")
    if spec.get("deliverable"):
        parts.append(f"\n\n--- 交付要求 ---\n{spec['deliverable']}")
    if spec.get("deliverable_schema"):
        parts.append("\n\n只输出满足以下 JSON Schema 的 JSON，不要输出其他文字：\n" + canonical_json(spec["deliverable_schema"]))
    return "".join(parts)
