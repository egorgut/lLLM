from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ollama import Client

from config import (
    DEFAULT_REASONING_MODE,
    OLLAMA_HOST,
    ModelProfile,
    resolve_reasoning_think,
)


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


@dataclass(frozen=True)
class ModelStreamChunk:
    """One fragment of a streamed model message, with reasoning kept apart.

    The transport-level distinction SPEC-020 §4.3 asks for, and nothing more: a
    chunk carries hidden reasoning, user-visible content, or (for a chunk that
    only announced a tool call) neither. The two are never concatenated, because
    the whole point is that the caller can stream one to the terminal and keep
    the other out of it. Not a Qwen-specific parser -- a non-thinking model
    simply never produces a chunk with `thinking` set.
    """

    thinking: str = ""
    content: str = ""


@dataclass(frozen=True)
class ModelResponseMetrics:
    """Ollama's own timing/count metadata for one finished response (SPEC-020 §4.10).

    Diagnostic only: nothing here alters a deadline or any turn policy. It exists
    so a long silence can be attributed -- model load, prompt evaluation, or token
    generation -- instead of guessed at.

    Durations arrive from the SDK in nanoseconds and are stored as milliseconds.
    Every field is optional: Ollama sets them on the *final* streamed chunk, and a
    response the caller stops reading early (a tool call is authoritative the
    moment it is seen) never reaches that chunk, so those requests record `None`
    rather than a fabricated number.
    """

    load_ms: int | None = None
    prompt_eval_ms: int | None = None
    prompt_eval_count: int | None = None
    eval_ms: int | None = None
    eval_count: int | None = None

    @classmethod
    def from_chunk(cls, chunk: Any) -> "ModelResponseMetrics | None":
        """Read the metadata off a streamed chunk, or `None` if it carries none."""

        def ms(value: Any) -> int | None:
            return int(value / 1_000_000) if value else None

        metrics = cls(
            load_ms=ms(getattr(chunk, "load_duration", None)),
            prompt_eval_ms=ms(getattr(chunk, "prompt_eval_duration", None)),
            prompt_eval_count=getattr(chunk, "prompt_eval_count", None),
            eval_ms=ms(getattr(chunk, "eval_duration", None)),
            eval_count=getattr(chunk, "eval_count", None),
        )
        return metrics if metrics != cls() else None

    def as_trace_fields(self) -> dict[str, Any]:
        """The additive `ollama_*` fields one `model_response_finished` reports."""

        return {
            "ollama_load_ms": self.load_ms,
            "ollama_prompt_eval_ms": self.prompt_eval_ms,
            "ollama_prompt_eval_count": self.prompt_eval_count,
            "ollama_eval_ms": self.eval_ms,
            "ollama_eval_count": self.eval_count,
        }


# Distinguishes "the caller said nothing about thinking" from "the caller
# explicitly asked for the model default (None)". Only the first inherits the
# transport's configured reasoning mode; `text()` passing `think=False` -- or any
# future specialized call -- always wins (SPEC-020 §4.2).
_UNSET: Any = object()


class OllamaModel:
    """The model transport bound to one run's profile (SPEC-017).

    Built once by the entry point from the selected :class:`config.ModelProfile`
    and injected, the way every other component of this project receives its
    dependencies. Before SPEC-017 this module read the model name and built its
    client at import time, which made the model impossible to select per run.

    :meth:`respond` matches the ``Respond`` callable ``AgentRunner`` expects, and
    serves the router (no tool declarations) just as well.

    SPEC-020 adds one piece of run configuration here: the agent-side reasoning
    mode. It belongs at this composition boundary rather than in the loop --
    `AgentRunner` should no more choose how hard the model thinks than it chooses
    which model answers. `respond()` applies it; `text()` overrides it explicitly
    and unconditionally, which is what lets one transport object keep serving both
    roles when SPEC-019 resolves them to the same profile.
    """

    def __init__(
        self,
        model: str,
        client: Client,
        *,
        reasoning_think: bool | str | None = None,
    ) -> None:
        self.model = model
        self._client = client
        # Already resolved from a ReasoningMode: the transport stores the value
        # the SDK takes, not the host's name for it.
        self._reasoning_think = reasoning_think

    @classmethod
    def for_profile(
        cls,
        profile: ModelProfile,
        *,
        host: str = OLLAMA_HOST,
        reasoning_mode: str = DEFAULT_REASONING_MODE,
    ) -> "OllamaModel":
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
            reasoning_think=resolve_reasoning_think(reasoning_mode),
        )

    def respond(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        think: bool | str | None = _UNSET,
        response_format: dict[str, Any] | None = None,
    ) -> "ModelResponse":
        # Both extras are keyword-only, and the `Respond` callable AgentRunner
        # holds is unchanged in both signature and behavior: the loop passes
        # neither, so `think` falls back to this transport's configured reasoning
        # mode -- which under the default `auto` is None, the SDK's own default
        # meaning "the model decides" (SPEC-020 §4.1). An explicit argument always
        # wins, which is how `text()` stays authoritative for routing.
        return ModelResponse(
            messages,
            tools,
            model=self.model,
            client=self._client,
            think=self._reasoning_think if think is _UNSET else think,
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
        deliberation earns its cost *there* is a separate question -- SPEC-020 is
        where it gets asked. Note that `think=False` here is passed *explicitly*,
        so routing can never inherit an agent reasoning mode, whichever mode the
        run selected and whether or not the two roles share this object.

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
    """Drives one streaming Ollama chat response, separating reasoning, text, and
    tool calls.

    The same object serves both the first and the second request of a tool-
    assisted turn. Fragments are streamed through :meth:`chunks` as
    :class:`ModelStreamChunk`; any tool calls the model emits are collected into
    :attr:`tool_calls`.

    Before SPEC-020 the model's hidden reasoning (``message.thinking``) was not
    read at all. It is now surfaced *separately* from content, so a caller can
    keep it out of the terminal and out of storage while still using it as
    transient turn state. The transport takes no view on that: it never
    concatenates the two, and it never prints anything.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        model: str,
        client: Client,
        think: bool | str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        if not messages:
            raise ValueError("Message history cannot be empty.")

        self.tool_calls: list[ModelToolCall] = []
        # Ollama's own load/prompt-eval/eval numbers, available only once the
        # stream has run to its final chunk (SPEC-020 §4.10).
        self.metrics: ModelResponseMetrics | None = None
        self._stream = client.chat(
            model=model,
            messages=messages,
            tools=tools,
            stream=True,
            think=think,
            format=response_format,
        )

    def chunks(self) -> Iterator[ModelStreamChunk]:
        """Yield reasoning and text fragments as Ollama generates them.

        Stops as soon as the model emits a tool call: a tool call is authoritative
        for the turn, so nothing is yielded once one has been seen. That early
        stop is also why :attr:`metrics` stays `None` for a tool-emitting request
        -- the metadata rides on the final chunk, and draining the stream to reach
        it would mean waiting past the decision the caller already has.
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
                self.metrics = ModelResponseMetrics.from_chunk(chunk) or self.metrics
                break

            if message.thinking:
                yield ModelStreamChunk(thinking=message.thinking)

            if message.content:
                yield ModelStreamChunk(content=message.content)

            self.metrics = ModelResponseMetrics.from_chunk(chunk) or self.metrics

    def text_chunks(self) -> Iterator[str]:
        """The content-only view of :meth:`chunks`, for callers with no use for
        reasoning.

        Skill routing is exactly such a caller: it asks for no thinking in the
        first place (PATCH-012-01), so there is never anything here to drop.
        """

        for chunk in self.chunks():
            if chunk.content:
                yield chunk.content
