"""Transient terminal activity indicator for silent parts of a turn (PATCH-010-01).

Presentation only, and deliberately separate from every other layer: the model
transport, the agent loop, and the tools know nothing about it. The CLI owns one
indicator, starts it whenever it is about to be silent (skill routing, a model
decision, a tool execution) and stops it before printing anything durable.

The indicator shows *activity, not progress* — the harness cannot know how far a
model decision has come, so there is no percentage, ETA, or token count here.

Only `\\r` and spaces are used, never an ANSI escape sequence, and the animation
runs on one daemon thread that is joined before `stop()` returns, so no frame can
ever be interleaved with normal CLI output. When the stream is not a TTY the
whole thing is a no-op that writes nothing at all, which keeps redirected output,
test captures, and the transcripts committed to `README.md` byte-for-byte
unchanged.
"""

import sys
import threading
from typing import TextIO

FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
FRAME_INTERVAL_SECONDS = 0.12
LABEL = "Working..."
THREAD_NAME = "cli-activity"


class ActivityIndicator:
    """A one-line spinner shown while the CLI would otherwise be silent.

    `start()` and `stop()` are idempotent and safe to call from any thread: the
    agent loop reports tool calls and results on the main thread but streams
    final-answer chunks from its deadline worker thread (`agent.py`), so both
    reach the same indicator.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        frames: tuple[str, ...] = FRAMES,
        interval_seconds: float = FRAME_INTERVAL_SECONDS,
        label: str = LABEL,
        enabled: bool | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._frames = frames
        self._interval_seconds = interval_seconds
        self._label = label
        # Animation is for an interactive terminal only. A pipe, a file, or a
        # captured test stream gets nothing -- not even a static line -- so its
        # output stays exactly what it was before this patch.
        if enabled is None:
            isatty = getattr(self._stream, "isatty", None)
            enabled = bool(isatty()) if callable(isatty) else False
        self._enabled = enabled
        self._width = max(len(frame) for frame in frames) + 1 + len(label)
        # Serializes start/stop against each other; the animation thread never
        # takes this lock, so `stop()` can join while holding it.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """Show the indicator. A second call while active does nothing."""

        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            # Draw the first frame synchronously, on the calling thread: activity
            # must be visible the moment the turn starts, not one tick later.
            self._draw(0)
            self._thread = threading.Thread(
                target=self._animate, name=THREAD_NAME, daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Clear the indicator and join its thread. Safe when already stopped.

        The thread is joined *before* the line is erased, so a frame can never
        be written after this returns -- which is what lets every caller print
        durable output immediately afterwards.
        """

        if not self._enabled:
            return
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
            thread.join()
            self._thread = None
            self._write("\r" + " " * self._width + "\r")

    def __enter__(self) -> "ActivityIndicator":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        # Returns None, so an exception -- including a `KeyboardInterrupt`
        # raised mid-turn -- keeps propagating with the terminal already clean.
        self.stop()

    def _animate(self) -> None:
        index = 1
        # `Event.wait` rather than `sleep`: stopping is immediate, not delayed
        # by up to one frame interval.
        while not self._stop.wait(self._interval_seconds):
            self._draw(index)
            index += 1

    def _draw(self, index: int) -> None:
        frame = self._frames[index % len(self._frames)]
        self._write(f"\r{frame} {self._label}")

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:  # noqa: BLE001 - a broken stdout must not break a turn
            pass
