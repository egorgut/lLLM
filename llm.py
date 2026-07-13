from collections.abc import Iterator

from ollama import Client

from config import MODEL_NAME, OLLAMA_HOST


client = Client(host=OLLAMA_HOST)


def stream_chat_with_model(messages: list[dict[str, str]]) -> Iterator[str]:
    """Yield assistant response text fragments as Ollama generates them.

    This is a generator, so the body — including the validation below — runs
    only when the caller first iterates, not when the generator is created.
    """

    if not messages:
        raise ValueError("Message history cannot be empty.")

    response_stream = client.chat(
        model=MODEL_NAME,
        messages=messages,
        stream=True,
    )

    for chunk in response_stream:
        content = chunk.message.content
        if content:
            yield content
