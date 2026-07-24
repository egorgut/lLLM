from evals.runner import DEFAULT_CASES_PATH, evaluate_expectation, load_cases, run_suite
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
            "timeout",
            "skill_explicit",
            "skill_auto",
            "skill_none",
            "skill_clarification",
            "skill_policy_violation",
            "skill_routing_repair",
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
