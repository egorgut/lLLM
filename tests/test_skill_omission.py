"""`app.omitted_skills` tests (PATCH-018-01).

The rule itself is not new — it was inline in `app.main()` since SPEC-013 and
SPEC-016. It became a named function so the skill-aware live evaluation path
composes the *same* skill registry the application does; these tests pin the
behaviour that was moved, so the extraction cannot drift from either caller.
"""

from app import omitted_skills
from config import TRACKER_MCP_SERVER_ID
from mcp_integration.config import McpServerConfig
from sandbox_tool import SKILL_NAME as SANDBOX_SKILL_NAME


def servers(*, tracker_enabled: bool) -> dict[str, McpServerConfig]:
    return {
        TRACKER_MCP_SERVER_ID: McpServerConfig(
            server_id=TRACKER_MCP_SERVER_ID,
            command="uvx",
            args=(),
            env={},
            enabled=tracker_enabled,
        )
    }


class _Sandbox:
    """Stands in for a built `SandboxCapability`; only its presence matters."""


def test_everything_available_omits_nothing():
    assert omitted_skills(servers(tracker_enabled=True), _Sandbox()) == frozenset()


def test_disabled_tracker_omits_tracker_read():
    assert omitted_skills(servers(tracker_enabled=False), _Sandbox()) == frozenset(
        {"tracker_read"}
    )


def test_unavailable_sandbox_omits_the_sandbox_skill():
    assert omitted_skills(servers(tracker_enabled=True), None) == frozenset(
        {SANDBOX_SKILL_NAME}
    )


def test_both_unavailable_omits_both():
    assert omitted_skills(servers(tracker_enabled=False), None) == frozenset(
        {"tracker_read", SANDBOX_SKILL_NAME}
    )
