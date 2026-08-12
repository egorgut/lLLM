"""Startup validation of the host-owned skill configuration (SPEC-012 §"Reliability
integration"). Mirrors ``reliability.validate_reliability_config``: all bounds are
host-owned, the model never supplies them, so an incoherent value is a deployment
defect raised as a plain ``ValueError`` before the chat loop starts."""


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
