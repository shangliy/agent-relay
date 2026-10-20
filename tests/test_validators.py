from __future__ import annotations

import pytest

from relay.templates import TEMPLATES
from relay.validators import check_params, count_words, run_validator


def test_count_words_mixed():
    assert count_words("hello world") == 2
    assert count_words("中文测试 and English") == 6


def test_json_schema_validator_with_fence():
    schema = TEMPLATES["paper_idea"]["deliverable_schema"]
    good = '```json\n{"ideas":[{"title":"t","hypothesis":"h","baselines":["b"],"minimal_experiment":"m"}]}\n```'
    assert run_validator("json_schema", "1", {"schema": schema}, good).passed
    bad = run_validator("json_schema", "1", {"schema": schema}, '{"ideas":[]}')
    assert not bad.passed and bad.checks[-1]["name"] == "json_schema"


def test_empty_output_always_fails():
    assert not run_validator("json", "1", {}, "   ").passed


def test_pinned_version_required():
    with pytest.raises(ValueError):
        run_validator("nonempty", "2", {}, "x")


def test_param_checks():
    with pytest.raises(ValueError):
        check_params("json_schema", {})
    with pytest.raises(ValueError):
        check_params("min_words", {"min": 0})
    with pytest.raises(ValueError):
        check_params("nope", {})
