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
    ):
        if value < 1:
            raise ValueError(f"{name} must be at least 1, got {value}.")
