"""Startup validation of the host-owned skill configuration (SPEC-012 §"Reliability
integration"). Mirrors ``reliability.validate_reliability_config``: all bounds are
host-owned, the model never supplies them, so an incoherent value is a deployment
defect raised as a plain ``ValueError`` before the chat loop starts."""

from collections.abc import Sequence

from tools import ToolRegistry


def validate_skill_config(
    *,
    skill_routing_timeout_seconds: float,
    skill_routing_repair_attempts: int,
    max_skill_routing_response_chars: int,
    max_skill_instruction_chars: int,
    max_skill_schema_bytes: int,
    max_skills: int,
    max_skill_description_chars: int,
    max_skill_activations_per_turn: int,
) -> None:
    if skill_routing_timeout_seconds <= 0:
        raise ValueError(
            "skill_routing_timeout_seconds must be > 0, got "
            f"{skill_routing_timeout_seconds}."
        )
    if skill_routing_repair_attempts < 0:
        raise ValueError(
            "skill_routing_repair_attempts must be >= 0, got "
            f"{skill_routing_repair_attempts}."
        )
    for name, value in (
        ("max_skill_routing_response_chars", max_skill_routing_response_chars),
        ("max_skill_instruction_chars", max_skill_instruction_chars),
        ("max_skill_schema_bytes", max_skill_schema_bytes),
        ("max_skills", max_skills),
        ("max_skill_description_chars", max_skill_description_chars),
        # 0 is rejected rather than read as "disable mid-turn activation": the
        # declaration would still be offered to the model, so every call would
        # fail. Removing the capability is a code decision, not a limit of 0.
        ("max_skill_activations_per_turn", max_skill_activations_per_turn),
    ):
        if value < 1:
            raise ValueError(f"{name} must be at least 1, got {value}.")


def validate_baseline_tools(
    baseline_tool_names: Sequence[str], tool_registry: ToolRegistry
) -> None:
    """Check the host's baseline tool names against the final tool registry.

    Separate from ``validate_skill_config`` because this is the one piece of
    skill configuration that cannot be checked at import time: MCP tools are only
    registered once their servers have started, so the caller must run this after
    MCP registration, against the same registry the skills are validated against
    (PATCH-012-02).

    A name the registry does not know is a deployment mistake — a disabled server,
    a renamed remote tool, a typo — and silently dropping it would quietly remove
    a capability every active skill is supposed to keep. So it fails startup, the
    same way an unknown ``allowed_tools`` entry in a skill package does.
    """

    seen: set[str] = set()
    for name in baseline_tool_names:
        if name in seen:
            raise ValueError(f"Baseline tool '{name}' is configured more than once.")
        seen.add(name)
        if name not in tool_registry:
            raise ValueError(
                f"Baseline tool '{name}' is not registered. Host baseline tools "
                "must exist in the tool registry after MCP registration."
            )
