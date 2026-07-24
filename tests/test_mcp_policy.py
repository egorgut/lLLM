"""Pure admission-filtering algorithm tests (SPEC-013 §"Admission-policy tests").

`filter_discovered_tools` has no I/O, no session, and no subprocess, so every
case here runs against a plain in-memory fake tool list -- no live model,
MCP server, or network is involved.
"""

import pytest

from mcp_integration.policy import McpAdmissionError, filter_discovered_tools

APPROVED = frozenset(
    {"issue_get", "issues_find", "queue_get_metadata", "issue_get_comments"}
)


class FakeMcpTool:
    def __init__(self, name: str, description: str = "A test tool.") -> None:
        self.name = name
        self.description = description
        self.inputSchema = {"type": "object", "properties": {}}


def _catalog() -> list[FakeMcpTool]:
    return [
        FakeMcpTool("issue_get"),
        FakeMcpTool("issues_find"),
        FakeMcpTool("queue_get_metadata"),
        FakeMcpTool("issue_get_comments"),
        FakeMcpTool("issue_add_comment"),
        FakeMcpTool("issue_update"),
        FakeMcpTool("issue_create"),
        FakeMcpTool("issue_execute_transition"),
        FakeMcpTool("queues_get_all"),
        FakeMcpTool("users_get_all"),
    ]


def test_exactly_the_approved_tools_are_admitted():
    result = filter_discovered_tools(
        "tracker", _catalog(), allowed_tools=APPROVED, required_tools=APPROVED
    )
    admitted_upstream = {name for _, _, name in result.route_entries}
    assert admitted_upstream == APPROVED
    assert len(result.admitted_specs) == 4
    assert set(result.filtered_names) == {
        "issue_add_comment",
        "issue_update",
        "issue_create",
        "issue_execute_transition",
        "queues_get_all",
        "users_get_all",
    }
    assert result.missing_required_names == frozenset()


def test_admitted_names_are_correctly_namespaced():
    result = filter_discovered_tools(
        "tracker", _catalog(), allowed_tools=APPROVED, required_tools=APPROVED
    )
    model_facing_names = {name for name, _, _ in result.route_entries}
    assert model_facing_names == {
        "mcp_tracker__issue_get",
        "mcp_tracker__issues_find",
        "mcp_tracker__queue_get_metadata",
        "mcp_tracker__issue_get_comments",
    }
    for spec in result.admitted_specs:
        assert spec.name in model_facing_names


@pytest.mark.parametrize("missing_name", sorted(APPROVED))
def test_each_required_tool_is_individually_detected_as_missing(missing_name):
    catalog = [tool for tool in _catalog() if tool.name != missing_name]
    result = filter_discovered_tools(
        "tracker", catalog, allowed_tools=APPROVED, required_tools=APPROVED
    )
    assert result.missing_required_names == frozenset({missing_name})


def test_discovery_order_does_not_change_admitted_or_filtered_sets():
    forward = filter_discovered_tools(
        "tracker", _catalog(), allowed_tools=APPROVED, required_tools=APPROVED
    )
    reversed_result = filter_discovered_tools(
        "tracker", list(reversed(_catalog())), allowed_tools=APPROVED, required_tools=APPROVED
    )
    forward_upstream = {name for _, _, name in forward.route_entries}
    reversed_upstream = {name for _, _, name in reversed_result.route_entries}
    assert forward_upstream == reversed_upstream
    assert set(forward.filtered_names) == set(reversed_result.filtered_names)


def test_case_sensitive_match_does_not_admit_wrong_case():
    catalog = [FakeMcpTool("Issue_Get")]
    result = filter_discovered_tools(
        "tracker", catalog, allowed_tools=APPROVED, required_tools=frozenset()
    )
    assert result.admitted_specs == ()
    assert result.filtered_names == ("Issue_Get",)


def test_similar_or_prefixed_name_is_not_admitted():
    catalog = [FakeMcpTool("issue_get_v2"), FakeMcpTool("issue_getx")]
    result = filter_discovered_tools(
        "tracker", catalog, allowed_tools=APPROVED, required_tools=frozenset()
    )
    assert result.admitted_specs == ()
    assert set(result.filtered_names) == {"issue_get_v2", "issue_getx"}


def test_duplicate_upstream_name_is_a_controlled_error():
    catalog = [FakeMcpTool("issue_get"), FakeMcpTool("issue_get")]
    with pytest.raises(McpAdmissionError) as excinfo:
        filter_discovered_tools(
            "tracker", catalog, allowed_tools=APPROVED, required_tools=frozenset()
        )
    assert excinfo.value.error_type == "mcp_tool_name_collision"


def test_allowed_tools_none_admits_everything_unfiltered():
    # This is the trusted local `time`-server case: no allowlist means no
    # filtering, preserving today's behavior exactly.
    catalog = [FakeMcpTool("get_current_time"), FakeMcpTool("anything_else")]
    result = filter_discovered_tools(
        "time", catalog, allowed_tools=None, required_tools=frozenset()
    )
    assert len(result.admitted_specs) == 2
    assert result.filtered_names == ()


def test_missing_required_checked_against_admitted_not_merely_discovered():
    # A tool that exists upstream but is not in allowed_tools still counts
    # as missing if it is required -- required_tools must be a real subset
    # invariant enforced by McpServerConfig, but the algorithm itself must
    # not treat "discovered anywhere" as "admitted".
    catalog = [FakeMcpTool("issue_get")]
    result = filter_discovered_tools(
        "tracker",
        catalog,
        allowed_tools=frozenset({"issue_get"}),
        required_tools=frozenset({"issue_get", "issues_find"}),
    )
    assert result.missing_required_names == frozenset({"issues_find"})
