"""Structured local tracing for the agent loop (SPEC-011).

Traces are append-only JSON Lines, one complete JSON object per line, written
under `data/traces/`. This module is intentionally separate from CLI
rendering (`app.py`'s `CliRenderer`): a `TraceSink` never prints normal
assistant output, and the renderer never parses trace events. Both sides
observe the same turn independently.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from reliability import canonical_json, sha256_of

SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    """A timezone-aware UTC timestamp, millisecond precision, `Z` suffix."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_event(event: str, *, run_id: str, **fields: Any) -> dict[str, Any]:
    """Stamp one trace event with the fields every event must carry (§6)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_now_iso(),
        "event": event,
        "run_id": run_id,
        **fields,
    }


def preview_and_hash(value: Any, *, limit: int) -> tuple[str, str, bool]:
    """A size-bounded preview of `value` plus a stable hash of the full value.

    The hash is computed over the complete canonical encoding (for
    correlation across truncated previews), while the preview itself is cut to
    `limit` characters (§8). The hash is for correlation, not security.
    """

    encoded = canonical_json(value)
    digest = sha256_of(encoded)
    if len(encoded) <= limit:
        return encoded, digest, False
    return encoded[:limit], digest, True


class TraceSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class NullTraceSink:
    """Tracing disabled: every event is discarded."""

    def emit(self, event: dict[str, Any]) -> None:
        pass


class MemoryTraceSink:
    """An in-process sink for tests: events are kept in a list, in order."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class JsonlTraceSink:
    """Appends one JSON object per line to a local file.

    The file is never rewritten, only appended to. A single lock serializes
    writes so two events can never merge onto one line.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class SafeTraceSink:
    """Wraps any `TraceSink` so a tracing failure never breaks the agent.

    On the first failure, warns once via `on_warning` and makes one
    best-effort attempt to also emit a `trace_error` event describing it; that
    attempt is itself guarded so a broken sink can never recurse into repeated
    failures. Later failures in the same turn are silently absorbed — the
    warning is shown at most once per instance (SPEC-011 §19).
    """

    def __init__(
        self,
        inner: TraceSink,
        run_id: str,
        on_warning: Callable[[str], None] = lambda message: print(f"Warning: {message}"),
    ) -> None:
        self._inner = inner
        self._run_id = run_id
        self._on_warning = on_warning
        self._warned = False

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self._inner.emit(event)
        except Exception as error:
            if not self._warned:
                self._warned = True
                self._on_warning("trace output is unavailable for this run.")
            try:
                self._inner.emit(
                    build_event(
                        "trace_error",
                        run_id=self._run_id,
                        failed_event=event.get("event"),
                        error_type=type(error).__name__,
                    )
                )
            except Exception:
                pass
