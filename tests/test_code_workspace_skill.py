"""`code_workspace` skill package tests (SPEC-016 §12, §21.1).

Loads the real committed `skills/code_workspace/` package against a fake tool
registry, so a drift between SKILL.md's front matter and the tool contract fails
here rather than at someone's startup. No live model, Docker, or MCP involved.

The allowlist is the whole point of the package: `sandbox_execute` exists only
inside this skill, and this skill can reach nothing else. Both halves of that
are tested, in both directions.
"""

from pathlib import Path

import pytest

from config import SKILLS_ROOT
from conversation import Conversation
from reliability import SkillPolicyViolation, TerminationReason, TurnStatus
from skill_runtime.loader import SkillPackageLoader
from skill_runtime.models import SkillSelection
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.policy import RestrictedToolExecutor, declarations_for_names
from support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    ScriptedSkillRouter,
    make_tool_call,
    make_tool_registry,
)
from tracing import NullTraceSink

OTHER_TOOLS = (
    "sql_query",
    "python_calculate",
    "mcp_time__get_current_time",
    "mcp_tracker__issue_get",
    "mcp_tracker__issues_find",
    "mcp_tracker__queue_get_metadata",
    "mcp_tracker__issue_get_comments",
)


def _registry(with_sandbox: bool = True):
    names = OTHER_TOOLS + (("sandbox_execute",) if with_sandbox else ())
    return make_tool_registry(*names)


def _load_spec():
    skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), _registry())
    return skill_registry.get("code_workspace")


class TestPackage:
    def test_loads_from_the_real_skills_directory(self):
        assert _load_spec().name == "code_workspace"

    def test_allows_exactly_sandbox_execute(self):
        assert _load_spec().allowed_tools == ("sandbox_execute",)

    def test_allows_no_other_tool(self):
        spec = _load_spec()
        assert set(OTHER_TOOLS).isdisjoint(spec.allowed_tools)

    def test_documents_the_sandbox_paths_the_model_must_use(self):
        instruction = _load_spec().instruction
        assert "/sandbox/input" in instruction
        assert "/sandbox/output" in instruction

    def test_documents_the_hard_limits_of_the_sandbox(self):
        instruction = _load_spec().instruction.lower()
        for limitation in ("no network", "no package installation", "host filesystem"):
            assert limitation in instruction

    def test_documents_that_each_run_starts_empty(self):
        """PATCH-016-01: the rule must be stated where the tool is described.

        SPEC-016's live run showed the model discovering this by running into
        it twice, so "inputs come only from this call" has to be a stated fact
        rather than something a FileNotFoundError teaches.
        """

        instruction = _load_spec().instruction.lower()
        assert "each run starts empty" in instruction
        assert "input_files" in instruction
        assert "earlier turn" in instruction

    def test_names_cross_turn_access_as_non_correctable(self):
        """PATCH-016-01: it must appear in the retry rule, not only as a 'never'.

        The retry decision is where the wasted call was spent, so the guidance
        has to be readable at that moment.
        """

        instruction = _load_spec().instruction.lower()
        retry_rule = instruction.split("do not retry a failure no script can fix")
        assert len(retry_rule) == 2, "the non-correctable rule is missing"
        assert "earlier turn" in retry_rule[1].split("\n\n")[0]

    def test_offers_an_alternative_to_the_user(self):
        instruction = _load_spec().instruction.lower()
        assert "recreate" in instruction
        assert "supply it" in instruction or "supply the" in instruction

    def test_omitted_when_the_sandbox_is_unavailable(self):
        """Mirrors app.py: no sandbox tool registered means no skill loaded."""

        skill_registry = SkillPackageLoader().load_all(
            Path(SKILLS_ROOT),
            _registry(with_sandbox=False),
            omit=frozenset({"code_workspace"}),
        )
        assert not skill_registry.contains("code_workspace")
        assert skill_registry.contains("sales_analysis")

    def test_a_missing_tool_without_omission_still_fails_fast(self):
        """Omission is a narrow host-owned skip, not a relaxed validation mode."""

        from skill_runtime.loader import SkillPackageError

        with pytest.raises(SkillPackageError):
            SkillPackageLoader().load_all(
                Path(SKILLS_ROOT), _registry(with_sandbox=False)
            )


class TestToolVisibility:
    def test_only_sandbox_execute_is_declared_to_the_model(self):
        registry = _registry()
        declarations = declarations_for_names(registry, _load_spec().allowed_tools)
        assert [d["function"]["name"] for d in declarations] == ["sandbox_execute"]

    def test_sandbox_execute_is_absent_from_another_skills_declarations(self):
        registry = _registry()
        skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), registry)
        sales = skill_registry.get("sales_analysis")
        declarations = declarations_for_names(registry, sales.allowed_tools)
        assert "sandbox_execute" not in [d["function"]["name"] for d in declarations]


class TestRestrictedExecution:
    def _restricted(self, skill: str, handlers: dict):
        spec = SkillPackageLoader().load_all(Path(SKILLS_ROOT), _registry()).get(skill)
        inner = FakeToolExecutor(handlers)
        return (
            RestrictedToolExecutor(inner, frozenset(spec.allowed_tools), skill=skill),
            inner,
        )

    def test_sandbox_execute_passes_through_under_code_workspace(self):
        restricted, inner = self._restricted(
            "code_workspace",
            {"sandbox_execute": lambda args: {"ok": True, "status": "succeeded"}},
        )

        result = restricted.execute(
            "sandbox_execute", {"language": "python", "source": "print(1)"}
        )

        assert result["ok"] is True
        assert inner.calls == [
            ("sandbox_execute", {"language": "python", "source": "print(1)"})
        ]

    def test_sandbox_execute_is_rejected_under_another_skill(self):
        restricted, inner = self._restricted(
            "sales_analysis", {"sandbox_execute": lambda args: {"ok": True}}
        )

        with pytest.raises(SkillPolicyViolation) as error:
            restricted.execute(
                "sandbox_execute", {"language": "python", "source": "print(1)"}
            )

        assert error.value.requested_tool == "sandbox_execute"
        assert error.value.skill == "sales_analysis"
        assert inner.calls == []

    @pytest.mark.parametrize("tool", ["sql_query", "python_calculate", "mcp_tracker__issue_get"])
    def test_code_workspace_cannot_reach_any_other_tool(self, tool):
        restricted, inner = self._restricted(
            "code_workspace", {tool: lambda args: {"ok": True}}
        )

        with pytest.raises(SkillPolicyViolation):
            restricted.execute(tool, {})

        assert inner.calls == []


def _build_orchestrator(router, responder, handlers):
    registry = _registry()
    skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), registry)
    executor = FakeToolExecutor(handlers)
    orchestrator = SkillTurnOrchestrator(
        skill_registry=skill_registry,
        router=router,
        tool_registry=registry,
        executor=executor,
        respond=responder,
        renderer_factory=RecordingRenderer,
        default_tools=registry.to_ollama_tools(),
        run_id="run-1",
        max_tool_calls=4,
        max_identical_tool_calls=2,
        model_request_timeout_seconds=5,
        tool_execution_timeout_seconds=5,
        agent_turn_timeout_seconds=30,
        trace_sink=NullTraceSink(),
    )
    return orchestrator, executor


def _selection():
    return SkillSelection("code_workspace", "explicit request", "explicit", 0, 1)


def _succeeded(artifacts=()):
    return {
        "ok": True,
        "status": "succeeded",
        "exit_code": 0,
        "stdout": "done\n",
        "stderr": "",
        "artifacts": list(artifacts),
    }


class TestScriptedTurn:
    def test_a_selected_turn_runs_the_sandbox_and_answers(self):
        router = ScriptedSkillRouter(_selection())
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call(
                            "sandbox_execute",
                            {"language": "python", "source": "open('out.csv','w')"},
                        )
                    ]
                ),
                ScriptedModelResponse(
                    text="I created data/artifacts/run-1/turn-1/out.csv."
                ),
            ]
        )
        orchestrator, executor = _build_orchestrator(
            router,
            responder,
            {"sandbox_execute": lambda args: _succeeded([{"name": "out.csv"}])},
        )
        conversation = Conversation()
        conversation.add_user_message("Use code_workspace to make me a CSV.")

        result = orchestrator.run_turn(conversation)

        assert result.selection.skill_name == "code_workspace"
        assert result.outcome.status is TurnStatus.COMPLETED
        assert [name for name, _ in executor.calls] == ["sandbox_execute"]

    def test_reaching_for_another_tool_stops_the_turn(self):
        router = ScriptedSkillRouter(_selection())
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})]
                )
            ]
        )
        orchestrator, executor = _build_orchestrator(router, responder, {})
        conversation = Conversation()
        conversation.add_user_message("Use code_workspace and query the database.")

        result = orchestrator.run_turn(conversation)

        assert result.outcome.status is TurnStatus.STOPPED
        assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
        assert executor.calls == []
