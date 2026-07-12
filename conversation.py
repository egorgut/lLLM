from prompts import SYSTEM_PROMPT


class Conversation:
    """Owns the full dialogue history for a single chat session."""

    def __init__(self, system_prompt: str = SYSTEM_PROMPT) -> None:
        self.system_prompt = system_prompt
        self.messages: list[dict[str, str]] = []
        self.reset()

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def remove_last_message(self) -> None:
        """Roll back the most recent message (e.g. after a failed LLM call).

        The system prompt is never removed, so history stays consistent.
        """

        if len(self.messages) > 1:
            self.messages.pop()

    def reset(self) -> None:
        """Clear the dialogue, keeping only the system prompt."""

        self.messages = [{"role": "system", "content": self.system_prompt}]
