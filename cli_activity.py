"""Transient terminal activity indicator for silent parts of a turn (PATCH-010-01).

Presentation only, and deliberately separate from every other layer: the model
transport, the agent loop, and the tools know nothing about it. The CLI owns one
indicator, starts it whenever it is about to be silent (skill routing, a model
decision, a tool execution) and stops it before printing anything durable.

The indicator shows *activity, not progress* — the harness cannot know how far a
model decision has come, so there is no percentage, ETA, or token count here.
What it does show, since PATCH-010-03, is how long the turn has been running.
That is a measured fact about the past rather than a prediction about the
remaining work, so it does not weaken the rule above; it answers the one question
a spinner alone cannot, given that a turn on this project ranges from about a
second to several minutes depending on the model profile.

Only `\\r` and spaces are used, never an ANSI escape sequence, and the animation
runs on one daemon thread that is joined before `stop()` returns, so no frame can
ever be interleaved with normal CLI output. When the stream is not a TTY the
whole thing is a no-op that writes nothing at all, which keeps redirected output,
test captures, and the transcripts committed to `README.md` byte-for-byte
unchanged.
"""

import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
FRAME_INTERVAL_SECONDS = 0.12
LABEL = "Working..."
THREAD_NAME = "cli-activity"
TURN_TIME_PREFIX = "[time]"


def format_elapsed(seconds: float) -> str:
    """A short, human-readable duration: `4.2s`, `52.6s`, `3m 21.4s`.

    One decimal is enough to see a turn move without the last digit flickering
    every frame. Past a minute the bare seconds stop being readable at a glance
    -- a `deep` turn may legitimately run for several -- so they are split out.
    """

    # Rounded first, then split: at 59.97s the two branches would otherwise
    # disagree and the line would read `60.0s` for one frame before `1m 00.0s`.
    seconds = round(max(seconds, 0.0), 1)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:04.1f}s"


def format_turn_time(seconds: float) -> str:
    """The one-line footer reporting what a finished turn cost.

    Shares `format_elapsed` with the live counter deliberately: the number the
    user watched tick and the number they are left with must not be formatted by
    two pieces of code that can drift apart.
    """

    return f"{TURN_TIME_PREFIX} {format_elapsed(seconds)}"


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._frames = frames
        self._interval_seconds = interval_seconds
        self._label = label
        self._clock = clock
        # Animation is for an interactive terminal only. A pipe, a file, or a
        # captured test stream gets nothing -- not even a static line -- so its
        # output stays exactly what it was before this patch.
        if enabled is None:
            isatty = getattr(self._stream, "isatty", None)
            enabled = bool(isatty()) if callable(isatty) else False
        self._enabled = enabled
        # How much to erase. Unlike the fixed width this used to be, the drawn
        # line now grows with the counter (`4.2s` -> `1m 04.2s`), so erasing a
        # constant number of spaces would leave the tail of the widest frame on
        # screen. Track the widest line actually drawn since the last stop.
        self._drawn_width = 0
        # When the current turn started, or None between turns. Stamped by
        # `__enter__` rather than by `start()`, because the indicator stops and
        # restarts once per durable line and the counter must measure the whole
        # turn rather than the current silence (PATCH-010-03).
        self._origin: float | None = None
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

    @property
    def elapsed_seconds(self) -> float:
        """How long the current turn has been running; 0.0 between turns."""

        origin = self._origin
        return 0.0 if origin is None else self._clock() - origin

    def start(self) -> None:
        """Show the indicator. A second call while active does nothing."""

        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            # A bare `start()` outside a `with` block still gets a counter, from
            # its own beginning; inside one, the turn's origin already stands.
            if self._origin is None:
                self._origin = self._clock()
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
            # Joining before reading `_drawn_width` is what makes it safe to
            # read without a lock: the only writer has finished.
            thread.join()
            self._thread = None
            self._write("\r" + " " * self._drawn_width + "\r")
            self._drawn_width = 0

    def __enter__(self) -> "ActivityIndicator":
        # One origin for the whole turn, stamped before the first frame so the
        # counter never restarts at a `[skill]`, `[tool ...]`, or `[result]` line.
        self._origin = self._clock()
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        # Returns None, so an exception -- including a `KeyboardInterrupt`
        # raised mid-turn -- keeps propagating with the terminal already clean.
        self.stop()
        self._origin = None

    def _animate(self) -> None:
        index = 1
        # `Event.wait` rather than `sleep`: stopping is immediate, not delayed
        # by up to one frame interval.
        while not self._stop.wait(self._interval_seconds):
            self._draw(index)
            index += 1

    def _draw(self, index: int) -> None:
        frame = self._frames[index % len(self._frames)]
        line = f"{frame} {self._label} {format_elapsed(self.elapsed_seconds)}"
        self._drawn_width = max(self._drawn_width, len(line))
        self._write(f"\r{line}")

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:  # noqa: BLE001 - a broken stdout must not break a turn
            pass
