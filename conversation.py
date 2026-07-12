from config import MAX_CONTEXT_MESSAGES
from prompts import SYSTEM_PROMPT


class Conversation:
    """Owns the dialogue history for a single chat session.

    Stored history contains only user/assistant messages and may grow without
    bound. The system prompt is never part of that history — it is prepended
    fresh, alongside a bounded window, only when talking to the model.
    """

    def __init__(
        self,
        system_prompt: str = SYSTEM_PROMPT,
        messages: list[dict[str, str]] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self._messages: list[dict[str, str]] = list(messages) if messages else []

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self._messages.append({"role": "assistant", "content": content})

    def remove_last_message(self) -> None:
        """Roll back the most recent message (e.g. after a failed LLM call)."""

        if self._messages:
            self._messages.pop()

    def reset(self) -> None:
        """Clear the dialogue history."""

        self._messages = []

    @property
    def stored_messages(self) -> list[dict[str, str]]:
        """The complete history, as persisted on disk."""

        return list(self._messages)

    @property
    def messages_for_model(self) -> list[dict[str, str]]:
        """What the model sees: system prompt + last MAX_CONTEXT_MESSAGES."""

        system_message = {"role": "system", "content": self.system_prompt}
        recent = self._messages[-MAX_CONTEXT_MESSAGES:]
        return [system_message, *recent]
