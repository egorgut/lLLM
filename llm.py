from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ollama import Client

from config import MODEL_NAME, OLLAMA_HOST


client = Client(host=OLLAMA_HOST)


@dataclass(frozen=True)
class ModelToolCall:
    """A tool call requested by the model, in harness-level terms.

    `id` is part of the general tool-call contract but is always ``None`` with the
    installed Ollama SDK (0.6.2), which does not assign per-call identifiers.
    """

    id: str | None
    name: str
    arguments: dict[str, Any]


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
    ) -> None:
        if not messages:
            raise ValueError("Message history cannot be empty.")

        self.tool_calls: list[ModelToolCall] = []
        self._stream = client.chat(
            model=MODEL_NAME,
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
