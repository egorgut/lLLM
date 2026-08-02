#!/usr/local/bin/python3 -I
"""The trusted holder process for one sandbox container (SPEC-015 §7).

A container exits as soon as its main process does, taking the ``/sandbox/output``
tmpfs with it. That is a problem for the runtime, because the *only* moment the
generated files exist is between the job finishing and the container being
removed — and there is deliberately no writable host bind mount to catch them.

So the container's main process is this: an idle wait that does nothing at all.
The host runs the untrusted script beside it with ``docker exec``, copies the
bounded output while this process keeps the tmpfs alive, then kills the whole
container.

This file is the only project code baked into the image. It reads no input,
opens no file, touches no network, and never executes user source.
"""

import signal
import threading

_stop = threading.Event()


def _handle_signal(signum, frame):  # noqa: ARG001 - required handler signature
    """Exit promptly on `docker stop`, without waiting for its SIGKILL timeout."""

    _stop.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # No timeout: the container's lifetime is bounded by the host, which kills it
    # on every path (success, failure, timeout, output cap, interrupt).
    _stop.wait()


if __name__ == "__main__":
    main()
