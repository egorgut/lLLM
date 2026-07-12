import json
from datetime import datetime, timezone
from pathlib import Path


STORE_VERSION = 1
CONVERSATION_ID = "default"


class JsonConversationStore:
    """Persists conversation history as JSON on disk.

    Only user/assistant messages are stored. The system prompt lives in
    ``prompts.py`` and is never written here. The Conversation domain object
    does not know these details — it just hands over its stored messages.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, str]]:
        """Return the stored messages, or an empty list to start fresh.

        Missing file → empty conversation.
        Invalid JSON → warn, preserve the corrupted file, empty conversation.
        """

        if not self.path.exists():
            return []

        try:
            with self.path.open(encoding="utf-8") as file:
                data = json.load(file)
            return data["messages"]
        except (json.JSONDecodeError, KeyError, OSError) as error:
            self._preserve_corrupted(error)
            return []

    def save(self, messages: list[dict[str, str]]) -> None:
        """Write the full stored history to disk, creating the dir if missing."""

        self.path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "version": STORE_VERSION,
            "conversation_id": CONVERSATION_ID,
            "updated_at": _utc_now_iso(),
            "messages": messages,
        }

        with self.path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _preserve_corrupted(self, error: Exception) -> None:
        """Rename the unreadable file aside so nothing is lost, then warn."""

        timestamp = _utc_now_iso().replace(":", "-")
        backup = self.path.with_name(
            f"{self.path.stem}.corrupted-{timestamp}{self.path.suffix}"
        )
        self.path.rename(backup)
        print(
            f"Warning: could not read {self.path} ({error}). "
            f"Preserved it as {backup.name} and started a fresh conversation."
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
