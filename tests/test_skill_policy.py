"""Tool filtering and restricted-execution tests (SPEC-012 §"Unit tests: tool restriction")."""

import pytest

from config import BASELINE_TOOL_NAMES
from reliability import SkillPolicyViolation
from skill_runtime.config_validation import validate_baseline_tools
from skill_runtime.policy import (
    RestrictedToolExecutor,
    compose_skill_toolset,
    declarations_for_names,
)
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


class TestComposeSkillToolset:
    """Baseline composition (PATCH-012-02) — one rule for both entry points."""

    CONTROL = {
        "type": "function",
        "function": {"name": "activate_skill", "description": "x", "parameters": {}},
    }

    def registry(self):
        return make_tool_registry(
            "mcp_tracker__issue_get",
            "mcp_tracker__issues_find",
            "sql_query",
            "mcp_time__get_current_time",
        )

    def test_skill_tools_keep_declared_order_then_baseline_then_control(self):
        toolset = compose_skill_toolset(
            self.registry(),
            ("mcp_tracker__issues_find", "mcp_tracker__issue_get"),
            ("mcp_time__get_current_time",),
            self.CONTROL,
        )

        assert toolset.names == (
            "mcp_tracker__issues_find",
            "mcp_tracker__issue_get",
            "mcp_time__get_current_time",
            "activate_skill",
        )
        assert [d["function"]["name"] for d in toolset.declarations] == list(
            toolset.names
        )
        assert toolset.skill_tools == (
            "mcp_tracker__issues_find",
            "mcp_tracker__issue_get",
        )
        assert toolset.baseline_tools == ("mcp_time__get_current_time",)

    def test_declarations_and_policy_are_the_same_set(self):
        toolset = compose_skill_toolset(
            self.registry(), ("sql_query",), ("mcp_time__get_current_time",), self.CONTROL
        )

        # The whole point of the type: the model can never see a tool the
        # executor rejects, nor the executor permit one never declared.
        assert toolset.allowed_tools == {
            d["function"]["name"] for d in toolset.declarations
        }
        assert toolset.allowed_tools == set(toolset.names)

    def test_a_baseline_tool_the_skill_also_declares_appears_once(self):
        toolset = compose_skill_toolset(
            self.registry(),
            ("mcp_time__get_current_time", "sql_query"),
            ("mcp_time__get_current_time",),
            self.CONTROL,
        )

        # Deduplicated to the *first* occurrence, so the skill's own order wins.
        assert toolset.names == (
            "mcp_time__get_current_time",
            "sql_query",
            "activate_skill",
        )

    def test_unknown_baseline_name_is_rejected_not_dropped(self):
        with pytest.raises(KeyError):
            compose_skill_toolset(self.registry(), ("sql_query",), ("not_a_tool",))

    def test_no_control_declaration_composes_registry_tools_only(self):
        toolset = compose_skill_toolset(
            self.registry(), ("sql_query",), ("mcp_time__get_current_time",)
        )

        assert toolset.names == ("sql_query", "mcp_time__get_current_time")
        assert toolset.allowed_tools == set(toolset.names)

    def test_empty_baseline_reproduces_the_spec_012_view(self):
        toolset = compose_skill_toolset(
            self.registry(), ("sql_query",), (), self.CONTROL
        )

        assert toolset.names == ("sql_query", "activate_skill")


class TestValidateBaselineTools:
    """§1 — an unknown host baseline name is a startup defect, never a silent drop."""

    def test_registered_names_pass(self):
        registry = make_tool_registry("mcp_time__get_current_time", "sql_query")

        validate_baseline_tools(("mcp_time__get_current_time",), registry)

    def test_unknown_name_fails_startup(self):
        registry = make_tool_registry("sql_query")

        with pytest.raises(ValueError) as excinfo:
            validate_baseline_tools(("mcp_time__get_current_time",), registry)

        assert "mcp_time__get_current_time" in str(excinfo.value)

    def test_duplicate_name_fails_startup(self):
        registry = make_tool_registry("mcp_time__get_current_time")

        with pytest.raises(ValueError) as excinfo:
            validate_baseline_tools(
                ("mcp_time__get_current_time", "mcp_time__get_current_time"), registry
            )

        assert "more than once" in str(excinfo.value)

    def test_the_shipped_baseline_is_exactly_the_time_tool(self):
        # The PATCH deliberately ships one baseline tool; widening it is a
        # repository change that should break this test first.
        assert BASELINE_TOOL_NAMES == ("mcp_time__get_current_time",)
