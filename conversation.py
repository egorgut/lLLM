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
    def latest_user_message(self) -> str:
        """The most recent user message's content, or '' if there is none.

        Used by the skill router to route the current request; the stored history
        is never mutated by reading it (SPEC-012 §14).
        """

        for message in reversed(self._messages):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    def messages_for_model(
        self, *, additional_system: str | None = None
    ) -> list[dict[str, str]]:
        """What the model sees: system prompt + last MAX_CONTEXT_MESSAGES.

        When ``additional_system`` is given (SPEC-012, a host-generated active-skill
        wrapper), it is appended to the system message content — joining the
        trusted system-level context, never added as a user message. The stored
        history is unchanged; the skill segment is ephemeral to this one turn.
        """

        content = self.system_prompt
        if additional_system:
            content = f"{content}\n\n{additional_system}"
        recent = self._messages[-MAX_CONTEXT_MESSAGES:]
        return [{"role": "system", "content": content}, *recent]
