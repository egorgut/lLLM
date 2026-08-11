from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ollama import Client

from config import OLLAMA_HOST, ModelProfile


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
    ) -> "ModelResponse":
        return ModelResponse(messages, tools, model=self.model, client=self._client)

    def text(self, messages: list[dict[str, Any]]) -> str:
        """One buffered, tool-less response — the shape skill routing needs."""

        return "".join(self.respond(messages).text_chunks())


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
    ) -> None:
        if not messages:
            raise ValueError("Message history cannot be empty.")

        self.tool_calls: list[ModelToolCall] = []
        self._stream = client.chat(
            model=model,
            messages=messages,
            tools=tools,
            stream=True,
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
