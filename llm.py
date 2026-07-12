from ollama import Client, ChatResponse

from config import MODEL_NAME, OLLAMA_HOST


client = Client(host=OLLAMA_HOST)


def chat_with_model(messages: list[dict[str, str]]) -> str:
    """Send the full conversation history to the local model."""

    if not messages:
        raise ValueError("Message history cannot be empty.")

    response: ChatResponse = client.chat(
        model=MODEL_NAME,
        messages=messages,
    )

    return response.message.content