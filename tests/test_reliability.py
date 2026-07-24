import threading
import time

import pytest

from reliability import (
    STATUS_BY_REASON,
    USER_MESSAGE_BY_REASON,
    DeadlineExceeded,
    SkillPolicyViolation,
    TerminationReason,
    TurnContext,
    TurnStatus,
    canonical_json,
    run_with_deadline,
    tool_call_fingerprint,
    validate_reliability_config,
)


def valid_config(**overrides):
    config = dict(
        model_request_timeout_seconds=120,
        tool_execution_timeout_seconds=30,
        agent_turn_timeout_seconds=180,
        max_tool_calls=4,
        max_identical_tool_calls=2,
    )
    config.update(overrides)
    return config


class TestToolCallFingerprint:
    def test_key_order_does_not_change_fingerprint(self):
        a = tool_call_fingerprint("sql_query", {"a": 1, "b": 2})
        b = tool_call_fingerprint("sql_query", {"b": 2, "a": 1})
        assert a == b

    def test_different_argument_values_differ(self):
        a = tool_call_fingerprint("sql_query", {"query": "SELECT 1"})
        b = tool_call_fingerprint("sql_query", {"query": "SELECT 2"})
        assert a != b

    def test_different_tool_name_differs(self):
        a = tool_call_fingerprint("sql_query", {"query": "SELECT 1"})
        b = tool_call_fingerprint("python_calculate", {"query": "SELECT 1"})
        assert a != b

    def test_canonical_json_is_compact_and_sorted(self):
        assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


class TestRunWithDeadline:
    def test_returns_result_when_fn_finishes_in_time(self):
        result = run_with_deadline(lambda: 1 + 1, timeout_seconds=1.0, thread_name="t")
        assert result == 2

    def test_reraises_fn_exception_when_it_finishes_in_time(self):
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            run_with_deadline(boom, timeout_seconds=1.0, thread_name="t")

    def test_raises_deadline_exceeded_when_fn_blocks_past_timeout(self):
        never_set = threading.Event()

        def blocks_forever():
            never_set.wait()
            return "unreachable"

        with pytest.raises(DeadlineExceeded):
            run_with_deadline(blocks_forever, timeout_seconds=0.02, thread_name="blocker")

    def test_abandoned_worker_does_not_block_process_exit(self):
        # The worker thread must be a daemon: a non-daemon thread left running
        # past the deadline would hang interpreter shutdown.
        never_set = threading.Event()
        start_count = threading.active_count()

        with pytest.raises(DeadlineExceeded):
            run_with_deadline(
                never_set.wait, timeout_seconds=0.01, thread_name="daemon-check"
            )

        # The thread may still be alive (we never terminate it), but it must be
        # a daemon so it cannot keep the process alive.
        daemon_threads = [
            t for t in threading.enumerate() if t.name == "daemon-check"
        ]
        assert all(t.daemon for t in daemon_threads)
        never_set.set()  # let it finish so it doesn't leak into other tests


class TestValidateReliabilityConfig:
    def test_valid_config_does_not_raise(self):
        validate_reliability_config(**valid_config())

    @pytest.mark.parametrize(
        "overrides",
        [
            {"model_request_timeout_seconds": 0},
            {"model_request_timeout_seconds": -1},
            {"tool_execution_timeout_seconds": 0},
            {"agent_turn_timeout_seconds": 0},
            {"max_tool_calls": 0},
            {"max_identical_tool_calls": 0},
        ],
    )
    def test_invalid_values_raise(self, overrides):
        with pytest.raises(ValueError):
            validate_reliability_config(**valid_config(**overrides))

    def test_turn_timeout_below_smallest_component_timeout_raises(self):
        with pytest.raises(ValueError):
            validate_reliability_config(
                **valid_config(
                    model_request_timeout_seconds=120,
                    tool_execution_timeout_seconds=30,
                    agent_turn_timeout_seconds=10,
                )
            )

    def test_turn_timeout_equal_to_smallest_component_timeout_is_allowed(self):
        validate_reliability_config(
            **valid_config(
                model_request_timeout_seconds=120,
                tool_execution_timeout_seconds=30,
                agent_turn_timeout_seconds=30,
            )
        )


class TestSkillTerminationReasons:
    def test_every_reason_has_a_status(self):
        for reason in TerminationReason:
            assert reason in STATUS_BY_REASON

    def test_skill_reason_status_mapping(self):
        assert STATUS_BY_REASON[TerminationReason.SKILL_ROUTING_TIMEOUT] is TurnStatus.TIMED_OUT
        assert STATUS_BY_REASON[TerminationReason.SKILL_ROUTING_ERROR] is TurnStatus.FAILED
        assert STATUS_BY_REASON[TerminationReason.INVALID_SKILL_SELECTION] is TurnStatus.FAILED
        assert STATUS_BY_REASON[TerminationReason.SKILL_LOAD_ERROR] is TurnStatus.FAILED
        assert STATUS_BY_REASON[TerminationReason.SKILL_POLICY_VIOLATION] is TurnStatus.STOPPED

    def test_skill_reasons_have_user_messages(self):
        for reason in (
            TerminationReason.SKILL_ROUTING_TIMEOUT,
            TerminationReason.SKILL_ROUTING_ERROR,
            TerminationReason.INVALID_SKILL_SELECTION,
            TerminationReason.SKILL_LOAD_ERROR,
            TerminationReason.SKILL_POLICY_VIOLATION,
        ):
            assert USER_MESSAGE_BY_REASON[reason]

    def test_policy_violation_carries_context(self):
        error = SkillPolicyViolation(
            "not allowed", requested_tool="mcp_time__get_current_time", skill="sales_analysis"
        )
        assert error.reason is TerminationReason.SKILL_POLICY_VIOLATION
        assert error.requested_tool == "mcp_time__get_current_time"
        assert error.skill == "sales_analysis"


class TestTurnContext:
    def test_is_frozen_and_carries_shared_budget(self):
        context = TurnContext(
            run_id="run-1", turn_id="turn-1", started_at=100.0, deadline=280.0
        )
        assert context.turn_id == "turn-1"
        assert context.deadline - context.started_at == 180.0
        with pytest.raises(Exception):
            context.turn_id = "other"  # frozen
