"""Tool filtering and restricted-execution tests (SPEC-012 §"Unit tests: tool restriction")."""

import pytest

from reliability import SkillPolicyViolation
from skill_runtime.policy import RestrictedToolExecutor, declarations_for_names
from tests.support import FakeToolExecutor, make_tool_registry


def test_declarations_only_allowed_in_order():
    registry = make_tool_registry("sql_query", "python_calculate", "mcp_time__get_current_time")
    declarations = declarations_for_names(registry, ["python_calculate", "sql_query"])
    names = [d["function"]["name"] for d in declarations]
    assert names == ["python_calculate", "sql_query"]  # skill-declared order preserved
    assert "mcp_time__get_current_time" not in names


def test_declarations_reject_unknown_name():
    registry = make_tool_registry("sql_query")
    with pytest.raises(KeyError):
        declarations_for_names(registry, ["sql_query", "not_a_tool"])


def test_declarations_are_deep_copied():
    registry = make_tool_registry("sql_query")
    declarations = declarations_for_names(registry, ["sql_query"])
    declarations[0]["function"]["parameters"]["injected"] = True
    again = declarations_for_names(registry, ["sql_query"])
    assert "injected" not in again[0]["function"]["parameters"]


def test_global_registry_unchanged_by_filtering():
    registry = make_tool_registry("sql_query", "python_calculate")
    declarations_for_names(registry, ["sql_query"])
    all_names = [t["function"]["name"] for t in registry.to_ollama_tools()]
    assert all_names == ["sql_query", "python_calculate"]


def test_allowed_execution_passes_through():
    inner = FakeToolExecutor({"sql_query": lambda args: {"ok": True, "rows": []}})
    restricted = RestrictedToolExecutor(
        inner, frozenset({"sql_query"}), skill="sales_analysis"
    )
    result = restricted.execute("sql_query", {"query": "SELECT 1"})
    assert result == {"ok": True, "rows": []}
    assert inner.calls == [("sql_query", {"query": "SELECT 1"})]


def test_disallowed_execution_rejected_before_handler():
    inner = FakeToolExecutor({"sql_query": lambda args: {"ok": True}})
    restricted = RestrictedToolExecutor(
        inner, frozenset({"sql_query"}), skill="sales_analysis"
    )
    with pytest.raises(SkillPolicyViolation) as excinfo:
        restricted.execute("mcp_time__get_current_time", {})
    assert excinfo.value.requested_tool == "mcp_time__get_current_time"
    assert excinfo.value.skill == "sales_analysis"
    # The underlying executor never saw the disallowed call.
    assert inner.calls == []
