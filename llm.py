from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ollama import Client

from config import OLLAMA_HOST, ModelProfile


# The shape a skill-routing response must have (PATCH-012-01). This is the same
# contract `SkillRouter._parse` validates -- an object with an exact catalog name
# in `skill`, or null when no skill applies -- restated for the model as a schema
# the server constrains generation to. Keep the two in step: `_parse` remains
# authoritative, because the schema cannot express "an exact name from *this*
# turn's catalog", and a response still has to survive it.
ROUTING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["skill", "reason"],
}


@dataclass(frozen=True)
class ModelToolCall:
    """A tool call requested by the model, in harness-level terms.

    `id` is part of the general tool-call contract but is always ``None`` with the
    installed Ollama SDK (0.6.2), which does not assign per-call identifiers.
    """

    id: str | None
    name: str
    arguments: dict[str, Any]


class OllamaModel:
    """The model transport bound to one run's profile (SPEC-017).

    Built once by the entry point from the selected :class:`config.ModelProfile`
    and injected, the way every other component of this project receives its
    dependencies. Before SPEC-017 this module read the model name and built its
    client at import time, which made the model impossible to select per run.

    :meth:`respond` matches the ``Respond`` callable ``AgentRunner`` expects, and
    serves the router (no tool declarations) just as well.
    """

    def __init__(self, model: str, client: Client) -> None:
        self.model = model
        self._client = client

    @classmethod
    def for_profile(cls, profile: ModelProfile, *, host: str = OLLAMA_HOST) -> "OllamaModel":
        # The client timeout is a component-native defense-in-depth floor
        # (SPEC-011 §14): the installed SDK (0.6.2) maps it to httpx's
        # connect/read/write/pool timeouts, which bound inactivity between
        # chunks but not the total duration of a long, continuously streaming
        # response. The authoritative bound on one full model decision is the
        # caller-side deadline `agent.py` applies around the whole streaming
        # exchange (`reliability.run_with_deadline`), not this client setting.
        return cls(
            profile.model,
            Client(host=host, timeout=profile.model_request_timeout_seconds),
        )

    def respond(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        think: bool | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> "ModelResponse":
        # Both extras are keyword-only and default to None -- the SDK's own
        # defaults, meaning "the model decides" and "unconstrained" -- so the
        # `Respond` callable AgentRunner holds is unchanged in both signature and
        # behavior. Only `text()` sets them (PATCH-012-01).
        return ModelResponse(
            messages,
            tools,
            model=self.model,
            client=self._client,
            think=think,
            response_format=response_format,
        )

    def text(self, messages: list[dict[str, Any]]) -> str:
        """One buffered, tool-less response — the shape skill routing needs.

        Thinking is disabled here and only here (PATCH-012-01). Routing is a
        narrow classification — "which one skill, if any, best matches this
        request?" (SPEC-012 §"Core architectural decisions" 5) — and on a
        multi-intent request the models spent an unbounded amount of wall-clock
        deliberating over it: measured on qwen3:8b, 59.6 s to emit a correct
        172-character answer behind 14,819 characters of hidden reasoning, and a
        diagnostic run that reached 14,760 tokens over 9m58s without finishing.
        Nothing bounded that: `MAX_SKILL_ROUTING_RESPONSE_CHARS` is checked after
        a response arrives, so it bounds what is accepted, never what is
        generated, and the profile's routing deadline could only kill the turn.
        Turning thinking off takes the same decision to 0.8 s on the same model.

        The agent loop keeps its reasoning: `respond()` is untouched, and whether
        deliberation earns its cost *there* is a separate question this does not
        answer.

        Generation is also constrained to `ROUTING_RESPONSE_SCHEMA`, because
        turning thinking off exposed a second failure: qwen3:32b then wrapped its
        JSON in a ```json fence on 5 of 6 runs, which `SkillRouter._parse`
        rejects. The decision was correct every time -- only its packaging was
        not -- but two fences in a row exhaust the single repair attempt and fail
        the turn, and `skill-live-sales-001` failed exactly that way on `deep`.
        Constraining the response fixed all 27 of 27 probe runs across the three
        profiles, `null` selections included.
        """

        return "".join(
            self.respond(
                messages, think=False, response_format=ROUTING_RESPONSE_SCHEMA
            ).text_chunks()
        )


class ModelResponse:
    """Drives one streaming Ollama chat response, separating streamed text from
    tool calls.

    The same object serves both the first and the second request of a tool-
    assisted turn. Text fragments are streamed through :meth:`text_chunks`; any
    tool calls the model emits are collected into :attr:`tool_calls`. The model's
    hidden reasoning (``message.thinking``) is never read, so it is never exposed.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        model: str,
        client: Client,
        think: bool | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        if not messages:
            raise ValueError("Message history cannot be empty.")

        self.tool_calls: list[ModelToolCall] = []
        self._stream = client.chat(
            model=model,
            messages=messages,
            tools=tools,
            stream=True,
            think=think,
            format=response_format,
        )

    def text_chunks(self) -> Iterator[str]:
        """Yield assistant text fragments as Ollama generates them.

        Stops as soon as the model emits a tool call: a tool call is authoritative
        for the turn, so no text is yielded once one has been seen.
        """

        for chunk in self._stream:
            message = chunk.message

            if message.tool_calls:
                self.tool_calls.extend(
                    ModelToolCall(
                        id=None,
                        name=call.function.name,
                        arguments=dict(call.function.arguments),
                    )
                    for call in message.tool_calls
                )
                break

            if message.content:
                yield message.content
