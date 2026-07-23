"""The bounded agent loop (SPEC-010).

`AgentRunner` is the smallest correct agent runtime on top of the project's
existing abstractions: it repeatedly gives control back to the model after every
tool result until the model produces a final textual answer, capped by a
host-owned maximum number of tool executions per user turn.

The runner owns *loop policy only*. It does not own persistent chat storage, CLI
commands, MCP process lifecycle, tool registration, or any tool implementation —
those stay with the caller. Model transport and rendering are injected so the
loop is deterministically testable without a live model.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol

from llm import ModelToolCall
from tools import ToolExecutor


class AgentTurnError(Exception):
    """A controlled failure that aborts the current user turn.

    Raised for policy violations the model cannot resolve by reasoning: an empty
    final response, more than one tool call in a single response, an exceeded
    tool-call budget, or invalid runner configuration. A structured tool *failure
    envelope* (``{"ok": false, ...}``) is not one of these — it is a valid
    observation the loop feeds back to the model.
    """


class ModelResponseLike(Protocol):
    """The slice of :class:`llm.ModelResponse` the loop depends on.

    Declared as a Protocol so tests can inject a scripted response with no live
    Ollama. ``text_chunks()`` streams assistant text as it arrives; ``tool_calls``
    is authoritative once the stream has been consumed.
    """

    def text_chunks(self) -> Iterator[str]: ...

    @property
    def tool_calls(self) -> list[ModelToolCall]: ...


class Renderer(Protocol):
    """Sink for user-visible loop output, injected to keep CLI concerns out."""

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None: ...

    def tool_result(self, result: dict) -> None: ...

    def text(self, chunk: str) -> None: ...


# messages, tool declarations -> one streaming model response.
Respond = Callable[[list[dict[str, Any]], Sequence[dict[str, Any]]], ModelResponseLike]


def assistant_tool_message(call: ModelToolCall) -> dict:
    """The temporary assistant message that records a tool call for the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.
    """

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
    }


def tool_result_message(call: ModelToolCall, result: dict) -> dict:
    """The temporary tool-result observation sent back to the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.
    """

    return {
        "role": "tool",
        "tool_name": call.name,
        "content": json.dumps(result, ensure_ascii=False),
    }


class AgentRunner:
    """Runs one user turn as a bounded model→tool→model loop.

    The caller supplies a *snapshot* of model-facing messages; the runner never
    receives the mutable ``Conversation``. Temporary tool-protocol messages live
    only in a per-turn working transcript and are discarded when the turn ends —
    the caller persists only the returned final answer.
    """

    def __init__(
        self,
        respond: Respond,
        executor: ToolExecutor,
        tools: Sequence[dict[str, Any]],
        max_tool_calls: int,
        renderer: Renderer,
    ) -> None:
        # The maximum is host-owned and must be usable: a value below 1 could
        # never execute a tool, so reject it at construction time (SPEC-010 §2).
        if max_tool_calls < 1:
            raise AgentTurnError(
                f"max_tool_calls must be at least 1, got {max_tool_calls}."
            )
        self._respond = respond
        self._executor = executor
        self._tools = tools
        self._max_tool_calls = max_tool_calls
        self._renderer = renderer

    def run_turn(self, messages: list[dict[str, Any]]) -> str:
        """Drive the loop until a final answer and return it.

        Raises :class:`AgentTurnError` for a controlled policy failure and lets an
        executor dispatch error (:class:`tools.executor.ToolExecutionError`)
        propagate; in both cases no partial answer is produced and the caller
        rolls back the turn. Structured ``{"ok": false}`` tool results do not stop
        the loop — the model may reason and recover from them.
        """

        working_messages = list(messages)
        tool_calls_used = 0

        while True:
            response = self._respond(working_messages, self._tools)

            # Stream assistant text as it arrives. A tool-selection response
            # carries no user-facing text, so nothing is rendered for it; only a
            # final textual answer streams (SPEC-010 §8).
            parts: list[str] = []
            for chunk in response.text_chunks():
                parts.append(chunk)
                self._renderer.text(chunk)
            text = "".join(parts)
            tool_calls = response.tool_calls

            if not tool_calls:
                if not text:
                    raise AgentTurnError("Model returned an empty response.")
                return text

            if len(tool_calls) != 1:
                raise AgentTurnError("Parallel tool calls are not supported.")

            # Enforce the budget before executing: the call that would exceed the
            # limit is never dispatched (SPEC-010 §2).
            if tool_calls_used >= self._max_tool_calls:
                raise AgentTurnError(
                    f"Agent stopped after {self._max_tool_calls} tool calls "
                    "without a final answer."
                )

            call = tool_calls[0]
            tool_calls_used += 1

            self._renderer.tool_call(call, tool_calls_used, self._max_tool_calls)
            result = self._executor.execute(call.name, call.arguments)
            self._renderer.tool_result(result)

            # Append the action and its observation to the working transcript so
            # the next model request sees every prior tool result from this turn.
            working_messages.extend(
                [
                    assistant_tool_message(call),
                    tool_result_message(call, result),
                ]
            )
