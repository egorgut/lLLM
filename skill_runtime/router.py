"""Skill routing: explicit user selection and bounded model routing (SPEC-012 §5-6, 16).

The router makes one narrow decision — *which single skill, if any, best matches
this request?* — and nothing else: it calls no tools, executes no task, produces
no user answer, and mutates no conversation history. It is separate from the
``AgentRunner`` and shares the same whole-turn deadline.

The model transport is injected as a ``RouteFn`` (messages -> raw text) so tests
drive a scripted router with no live Ollama. Output is strict JSON, size-bounded,
and validated against the exact catalog; a malformed first response earns at most
one repair request before the turn fails with a diagnosable routing error.
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from reliability import (
    DeadlineExceeded,
    InvalidSkillSelection,
    SkillRoutingError,
    SkillRoutingTimeout,
    run_with_deadline,
)
from skill_runtime.models import SkillCatalogEntry, SkillSelection
from tracing import NullTraceSink, TraceSink, build_event, preview_and_hash

# messages -> one raw model response string.
RouteFn = Callable[[list[dict[str, Any]]], str]

_MAX_REASON_CHARS = 500

ROUTER_SYSTEM_INSTRUCTION = (
    "You are a skill router.\n\n"
    "Choose zero or one skill from the supplied catalog for the current user "
    "request.\n\n"
    "Rules:\n"
    "- return null when no skill is necessary;\n"
    "- select only an exact catalog name;\n"
    "- do not solve the task;\n"
    "- do not call tools;\n"
    "- do not invent skills;\n"
    "- prefer no skill when the request is general conversation or can be "
    "answered without a specialized procedure;\n"
    '- return one JSON object only, of the form {"skill": <name-or-null>, '
    '"reason": <short string>}.'
)

# Conservative explicit-selection wrappers. Case-insensitive phrase, exact
# canonical skill token bounded by \b (underscore is a word char, so a near
# match like `sales_analysis_v2` never matches `sales_analysis`).
_EXPLICIT_TEMPLATES = (
    r"\buse\s+the\s+{name}\s+skill\b",
    r"\buse\s+skill\s+{name}\b",
    r"\bwith\s+the\s+{name}\s+skill\b",
)


class _RoutingParseError(Exception):
    """The model's routing output was malformed or named an unknown skill."""


def _with_count(error: Exception, routing_requests: int) -> Exception:
    """Annotate a raised routing error with the model requests spent so far.

    Lets the orchestrator report an accurate ``model_requests`` on the terminal
    outcome of a routing failure (routing requests + zero agent requests).
    """

    error.routing_requests = routing_requests  # type: ignore[attr-defined]
    return error


def parse_explicit_selection(
    user_message: str, catalog_names: Sequence[str]
) -> str | None:
    """Return an exact catalog name the user explicitly requested, else ``None``.

    Deliberately conservative: exact token, case-insensitive wrapper, no fuzzy
    matching and no path separators. A near match or unknown name yields ``None``
    (routing falls through to the model), never a silent substitution.
    """

    for name in catalog_names:
        escaped = re.escape(name)
        for template in _EXPLICIT_TEMPLATES:
            if re.search(template.format(name=escaped), user_message, re.IGNORECASE):
                return name
    return None


class SkillRouter:
    def __init__(
        self,
        route: RouteFn,
        *,
        timeout_seconds: float,
        max_response_chars: int,
        repair_attempts: int,
        clock: Callable[[], float] = time.monotonic,
        payload_preview_chars: int = 1000,
    ) -> None:
        self._route = route
        self._timeout_seconds = timeout_seconds
        self._max_response_chars = max_response_chars
        self._repair_attempts = repair_attempts
        self._clock = clock
        self._payload_preview_chars = payload_preview_chars

    def select(
        self,
        *,
        user_message: str,
        conversation_context: list[dict[str, Any]],
        catalog: Sequence[SkillCatalogEntry],
        deadline: float,
        run_id: str,
        turn_id: str,
        catalog_fingerprint: str = "",
        trace: TraceSink = NullTraceSink(),
    ) -> SkillSelection:
        start = self._clock()

        def elapsed_ms() -> int:
            return int((self._clock() - start) * 1000)

        # An empty catalog needs no model call at all (SPEC-012 §"router" test 9).
        if not catalog:
            trace.emit(
                build_event(
                    "skill_not_selected",
                    run_id=run_id,
                    turn_id=turn_id,
                    selection_source="none",
                    reason="empty_catalog",
                )
            )
            return SkillSelection(None, "No skills are available.", "none", 0, elapsed_ms())

        names = tuple(entry.name for entry in catalog)

        explicit = parse_explicit_selection(user_message, names)
        if explicit is not None:
            # An exact explicit request bypasses the routing model entirely.
            trace.emit(
                build_event(
                    "skill_routing_finished",
                    run_id=run_id,
                    turn_id=turn_id,
                    selected_skill=explicit,
                    selection_source="explicit",
                    routing_requests=0,
                    duration_ms=elapsed_ms(),
                    catalog_fingerprint=catalog_fingerprint,
                )
            )
            return SkillSelection(
                explicit, "Explicitly requested by the user.", "explicit", 0, elapsed_ms()
            )

        trace.emit(
            build_event(
                "skill_routing_started",
                run_id=run_id,
                turn_id=turn_id,
                catalog_size=len(names),
                catalog_fingerprint=catalog_fingerprint,
            )
        )

        messages = self._initial_messages(user_message, conversation_context, catalog)
        requests = 0
        last_error = "invalid response"
        for attempt in range(self._repair_attempts + 1):
            if attempt > 0:
                trace.emit(
                    build_event(
                        "skill_routing_repair_started",
                        run_id=run_id,
                        turn_id=turn_id,
                        attempt=attempt,
                        error=last_error,
                    )
                )

            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _with_count(SkillRoutingTimeout("Skill routing timed out."), requests)
            timeout = min(self._timeout_seconds, remaining)

            requests += 1
            current = messages
            try:
                raw = run_with_deadline(
                    lambda: self._route(current),
                    timeout_seconds=timeout,
                    thread_name="skill-router",
                )
            except DeadlineExceeded:
                raise _with_count(
                    SkillRoutingTimeout("Skill routing timed out."), requests
                ) from None
            except Exception:
                raise _with_count(
                    SkillRoutingError("Skill routing transport failed."), requests
                ) from None

            preview, digest, truncated = preview_and_hash(
                raw, limit=self._payload_preview_chars
            )
            trace.emit(
                build_event(
                    "skill_routing_response",
                    run_id=run_id,
                    turn_id=turn_id,
                    attempt=attempt,
                    response_preview=preview,
                    response_sha256=digest,
                    response_truncated=truncated,
                )
            )

            try:
                skill_name, reason = self._parse(raw, names)
            except _RoutingParseError as error:
                last_error = str(error)
                messages = messages + [
                    {"role": "assistant", "content": raw[: self._max_response_chars]},
                    self._repair_message(last_error, names),
                ]
                continue

            trace.emit(
                build_event(
                    "skill_routing_finished",
                    run_id=run_id,
                    turn_id=turn_id,
                    selected_skill=skill_name,
                    selection_source="model",
                    routing_requests=requests,
                    duration_ms=elapsed_ms(),
                    catalog_fingerprint=catalog_fingerprint,
                )
            )
            return SkillSelection(skill_name, reason, "model", requests, elapsed_ms())

        raise _with_count(
            InvalidSkillSelection(
                f"Skill routing returned an invalid selection after {requests} "
                f"request(s): {last_error}."
            ),
            requests,
        )

    def _initial_messages(
        self,
        user_message: str,
        conversation_context: list[dict[str, Any]],
        catalog: Sequence[SkillCatalogEntry],
    ) -> list[dict[str, Any]]:
        catalog_json = json.dumps(
            [{"name": e.name, "description": e.description} for e in catalog],
            ensure_ascii=False,
        )
        context_text = _render_context(conversation_context)
        user_content = (
            f"Catalog:\n{catalog_json}\n\n"
            f"Conversation so far:\n{context_text}\n\n"
            f"Current user request:\n{user_message}\n\n"
            "Return one JSON object."
        )
        return [
            {"role": "system", "content": ROUTER_SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]

    def _repair_message(self, error: str, names: Sequence[str]) -> dict[str, Any]:
        allowed = ", ".join(names)
        return {
            "role": "user",
            "content": (
                f"Your previous response was invalid: {error}. Respond with "
                'exactly one JSON object of the form {"skill": <name-or-null>, '
                '"reason": <short string>}. The skill must be null or one of these '
                f"exact names: [{allowed}]. Return null when no skill applies."
            ),
        }

    def _parse(self, raw: str, names: Sequence[str]) -> tuple[str | None, str]:
        if len(raw) > self._max_response_chars:
            raise _RoutingParseError("response exceeds the size limit")
        text = raw.strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as error:
            raise _RoutingParseError(f"not valid JSON ({error})") from None
        if not isinstance(obj, dict):
            raise _RoutingParseError("not a JSON object")
        if "skill" not in obj:
            raise _RoutingParseError("missing 'skill' field")

        reason = obj.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        reason = reason[:_MAX_REASON_CHARS]

        skill = obj["skill"]
        if skill is None:
            return None, reason or "No specialized skill is needed."
        if not isinstance(skill, str) or skill not in names:
            raise _RoutingParseError(f"'{skill}' is not an exact catalog name")
        return skill, reason or "Selected by the router."


def _render_context(conversation_context: list[dict[str, Any]]) -> str:
    if not conversation_context:
        return "(none)"
    lines = []
    for message in conversation_context:
        role = message.get("role", "?")
        content = str(message.get("content", ""))
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
