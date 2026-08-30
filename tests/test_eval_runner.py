import json

import pytest

from evals.runner import (
    DEFAULT_CASES_PATH,
    CaseResult,
    evaluate_expectation,
    load_cases,
    parse_args,
    run_suite,
    write_results,
)
from reliability import AgentTurnOutcome, TerminationReason, TurnStatus


def make_outcome(**overrides) -> AgentTurnOutcome:
    fields = dict(
        run_id="r",
        turn_id="t",
        status=TurnStatus.COMPLETED,
        reason=TerminationReason.FINAL_ANSWER,
        final_text="The result is 49132.",
        tool_calls_executed=1,
        model_requests=2,
        duration_ms=5,
        error_message=None,
    )
    fields.update(overrides)
    return AgentTurnOutcome(**fields)


class TestEvaluateExpectation:
    def test_all_objective_assertions_pass_on_a_matching_outcome(self):
        outcome = make_outcome()
        failures = evaluate_expectation(
            outcome,
            ["python_calculate"],
            {
                "status": "completed",
                "reason": "final_answer",
                "required_tools": ["python_calculate"],
                "allowed_tools": ["python_calculate"],
                "min_tool_calls": 1,
                "max_tool_calls": 1,
                "answer_matches": "49132",
                "max_duration_ms": 1000,
            },
        )
        assert failures == []

    def test_wrong_status_is_a_failure(self):
        outcome = make_outcome(status=TurnStatus.STOPPED)
        failures = evaluate_expectation(outcome, [], {"status": "completed"})
        assert len(failures) == 1

    def test_missing_required_tool_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(), [], {"required_tools": ["sql_query"]}
        )
        assert len(failures) == 1

    def test_disallowed_tool_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(), ["sql_query"], {"allowed_tools": ["python_calculate"]}
        )
        assert len(failures) == 1

    def test_answer_not_containing_substring_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(), [], {"answer_contains": ["nonexistent"]}
        )
        assert len(failures) == 1

    def test_non_completed_outcome_skips_answer_checks(self):
        outcome = make_outcome(status=TurnStatus.FAILED, final_text=None)
        failures = evaluate_expectation(
            outcome, [], {"status": "failed", "answer_contains": ["irrelevant"]}
        )
        assert failures == []

    def test_forbidden_tool_use_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(),
            ["mcp_time__get_current_time"],
            {"forbidden_tools": ["mcp_time__get_current_time"]},
        )
        assert len(failures) == 1

    def test_expected_selection_mismatch_is_a_failure(self):
        from skill_runtime.models import SkillSelection

        selection = SkillSelection("database_exploration", "r", "model", 1, 5)
        failures = evaluate_expectation(
            make_outcome(),
            [],
            {"expected_selection": "sales_analysis"},
            selection=selection,
        )
        assert len(failures) == 1

    def test_expected_selection_none_matches_no_skill(self):
        from skill_runtime.models import SkillSelection

        selection = SkillSelection(None, "no skill", "model", 1, 5)
        failures = evaluate_expectation(
            make_outcome(), [], {"expected_selection": None}, selection=selection
        )
        assert failures == []


class TestSkillActivationExpectations:
    """SPEC-018 turned one skill decision into two ends of one (PATCH-018-01)."""

    def test_final_skill_mismatch_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(),
            [],
            {"expected_final_skill": "sales_analysis"},
            final_skill="tracker_read",
        )
        assert len(failures) == 1

    def test_final_skill_match_passes(self):
        failures = evaluate_expectation(
            make_outcome(),
            [],
            {"expected_final_skill": "sales_analysis"},
            final_skill="sales_analysis",
        )
        assert failures == []

    def test_activation_count_mismatch_is_a_failure(self):
        failures = evaluate_expectation(
            make_outcome(), [], {"expected_activations": 1}, activations=0
        )
        assert len(failures) == 1

    def test_activation_count_match_passes(self):
        failures = evaluate_expectation(
            make_outcome(), [], {"expected_activations": 2}, activations=2
        )
        assert failures == []

    def test_skill_keys_are_ignored_when_absent_from_the_expectation(self):
        failures = evaluate_expectation(
            make_outcome(), [], {"status": "completed"}, final_skill=None, activations=0
        )
        assert failures == []


class TestCasesFile:
    def test_committed_cases_load_and_have_unique_stable_ids(self):
        cases = load_cases(DEFAULT_CASES_PATH)
        ids = [case["id"] for case in cases]
        assert len(ids) == len(set(ids))
        assert len(cases) >= 9

    def test_required_categories_are_all_present(self):
        cases = load_cases(DEFAULT_CASES_PATH)
        categories = {case["category"] for case in cases}
        assert categories == {
            "no_tool_answer",
            "calculator",
            "sql_single_query",
            "sql_recovery",
            "multi_tool",
            "mcp_time",
            "repetition_guard",
            "tool_call_budget_guard",
            "control_call_budget_guard",
            "timeout",
            "skill_explicit",
            "skill_auto",
            "skill_none",
            "skill_clarification",
            "skill_policy_violation",
            "skill_routing_repair",
            "skill_activation_none",
            "skill_activation_replace",
            "skill_activation_unknown",
            "skill_activation_cap",
            "skill_baseline_time",
            "skill_live_none",
            "skill_live_sales",
            "skill_live_tracker",
            "skill_live_cross",
            "skill_live_cross_explicit",
            "skill_live_activation_forced",
            "tracker_issue_lookup",
            "tracker_issue_search",
            "tracker_queue_metadata",
            "tracker_comment_summary",
            "tracker_multi_read",
            "tracker_read_only_refusal",
            "tracker_filtered_tool",
            "tracker_auth_error",
            "tracker_permission_error",
            "tracker_not_found",
            "tracker_large_result",
            "tracker_prompt_injection_content",
            "tracker_live_smoke",
            "sandbox_python_artifact",
            "sandbox_bash_transform",
            "sandbox_error_recovery",
            "sandbox_network_refusal",
            "sandbox_package_refusal",
            "sandbox_host_path_refusal",
            "sandbox_not_selected",
            "sandbox_budget_guard",
            "sandbox_cross_turn_refusal",
        }


class TestScriptedSuiteRuns:
    def test_scripted_suite_passes_without_ollama_or_mcp(self):
        summary, results = run_suite("scripted", DEFAULT_CASES_PATH)

        assert summary["failed"] == 0
        assert summary["total"] == summary["passed"]
        assert summary["total"] >= 9
        for result in results:
            assert result.passed, (result.id, result.failures)

    def test_scripted_only_cases_run_in_the_scripted_suite(self):
        _, results = run_suite("scripted", DEFAULT_CASES_PATH)
        ids = {result.id for result in results}
        # repetition-guard-001, budget-guard-001, sql-recovery-001, and
        # timeout-scripted-001 are scripted-only by design (SPEC-011
        # §"Deterministic and live suites are separate") -- they must still
        # run in the scripted suite even though they're absent from "live".
        assert "repetition-guard-001" in ids
        assert "timeout-scripted-001" in ids


class TestLiveRoleSelection:
    """The live suite's two model roles (SPEC-019 §4.2, §4.10)."""

    def test_the_live_cli_accepts_the_same_override_as_the_app(self):
        args = parse_args(["--suite", "live", "--profile", "next", "--router-profile", "fast"])

        assert args.profile == "next"
        assert args.router_profile == "fast"

    def test_omitting_the_override_leaves_the_router_unset(self):
        assert parse_args(["--suite", "live", "--profile", "next"]).router_profile is None

    def test_an_unknown_router_profile_never_starts_a_run(self):
        with pytest.raises(SystemExit):
            parse_args(["--suite", "live", "--router-profile", "huge"])

    def test_scripted_suite_ignores_both_profile_flags(self):
        # The scripted suite never contacts a model and is pinned to
        # SCRIPTED_PROFILE, so neither flag may change its results.
        baseline, _ = run_suite("scripted", DEFAULT_CASES_PATH)
        from config import resolve_model_roles

        split, _ = run_suite(
            "scripted", DEFAULT_CASES_PATH, None, resolve_model_roles("next", "fast")
        )

        assert split == baseline
        assert split["failed"] == 0

    def test_results_serialize_both_role_identities(self, tmp_path, monkeypatch):
        import evals.runner as runner

        monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
        result = CaseResult(
            id="skill-live-sales-001",
            passed=True,
            status="completed",
            reason="final_answer",
            tool_calls=["sql_query"],
            duration_ms=1234,
            profile="next",
            router_profile="fast",
            router_model="qwen3:8b",
        )

        path = write_results("live", {"total": 1, "passed": 1, "failed": 0}, [result], "qwen3.8:27b")
        payload = json.loads(path.read_text(encoding="utf-8"))

        # The agent identity keeps its existing home, so an old reader is
        # unaffected; the router identity is additive beside it.
        assert payload["model"] == "qwen3.8:27b"
        assert payload["cases"][0]["profile"] == "next"
        assert payload["cases"][0]["router_profile"] == "fast"
        assert payload["cases"][0]["router_model"] == "qwen3:8b"


class TestLiveReasoningSettings:
    """The reasoning policy a live run measured under (SPEC-020 §4.13, §7.4)."""

    def test_the_live_cli_accepts_the_same_modes_as_the_app(self):
        assert parse_args(["--suite", "live"]).reasoning == "auto"
        assert parse_args(["--suite", "live", "--reasoning", "low"]).reasoning == "low"

    def test_an_unknown_mode_never_starts_a_run(self):
        with pytest.raises(SystemExit):
            parse_args(["--suite", "live", "--reasoning", "xhigh"])

    def test_preservation_is_on_unless_the_ab_flag_is_given(self):
        from evals.runner import ReasoningSettings

        assert ReasoningSettings().preserve is True
        assert parse_args(["--suite", "live"]).no_reasoning_preservation is False
        assert (
            parse_args(
                ["--suite", "live", "--no-reasoning-preservation"]
            ).no_reasoning_preservation
            is True
        )

    def test_scripted_suite_ignores_the_reasoning_flags(self):
        from evals.runner import ReasoningSettings

        baseline, _ = run_suite("scripted", DEFAULT_CASES_PATH)
        altered, _ = run_suite(
            "scripted",
            DEFAULT_CASES_PATH,
            None,
            None,
            ReasoningSettings(mode="medium", preserve=False),
        )

        assert altered == baseline

    def test_results_record_the_mode_and_the_metrics_but_no_reasoning(
        self, tmp_path, monkeypatch
    ):
        import evals.runner as runner

        monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
        result = CaseResult(
            id="multi-tool-001",
            passed=True,
            status="completed",
            reason="final_answer",
            tool_calls=["sql_query", "python_calculate"],
            duration_ms=1234,
            profile="next",
            reasoning_mode="low",
            preserve_reasoning=True,
            thinking_chars=[812, 0],
            first_model_output_ms=[640, 700],
            visible_ttft_ms=[8200, 900],
        )

        path = write_results("live", {"total": 1, "passed": 1, "failed": 0}, [result], "qwen3.8:27b")
        case = json.loads(path.read_text(encoding="utf-8"))["cases"][0]

        assert case["reasoning_mode"] == "low"
        assert case["preserve_reasoning"] is True
        assert case["thinking_chars"] == [812, 0]
        assert case["visible_ttft_ms"] == [8200, 900]
        # Counts and timings only: there is no field a reasoning string could
        # travel in, which is the point (SPEC-020 §4.11).
        assert not any("thinking" in key for key in case if key != "thinking_chars")
