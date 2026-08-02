"""Sandbox trace events on the existing tracing layer (SPEC-015 "Tracing").

There is no second sink, no second file format, and no second schema version
here — only the shape of the sandbox's own events. Everything is built with the
root ``tracing.build_event`` and written through whichever ``TraceSink`` the
caller supplied, wrapped in ``SafeTraceSink`` exactly as ``McpClientManager``
does, so a broken sink can never take down a job.

The events describe a job in metadata only. Source text, input bytes, artifact
bytes, host temporary paths, and host environment values are never recorded —
what a job *did* is traceable, what it *contained* is not.
"""

from typing import Any

from tracing import SafeTraceSink, TraceSink, build_event, preview_and_hash


class JobTracer:
    """Emits one job's events with the fields every sandbox event must carry.

    Created per job so ``job_id``, ``language``, ``turn_id``, and the policy
    fingerprint are stamped once rather than threaded through every call site in
    the backend. ``image_id`` is filled in after a successful preflight and
    appears on every later event.
    """

    def __init__(
        self,
        sink: TraceSink,
        *,
        run_id: str,
        job_id: str,
        language: str,
        policy_fingerprint: str,
        turn_id: str | None = None,
        preview_chars: int = 1000,
    ) -> None:
        self._sink = SafeTraceSink(sink, run_id)
        self._run_id = run_id
        self._job_id = job_id
        self._language = language
        self._policy_fingerprint = policy_fingerprint
        self._turn_id = turn_id
        self._preview_chars = preview_chars
        self._image_id: str | None = None

    def set_image_id(self, image_id: str) -> None:
        self._image_id = image_id

    def emit(self, event: str, **fields: Any) -> None:
        self._sink.emit(
            build_event(
                event,
                run_id=self._run_id,
                job_id=self._job_id,
                turn_id=self._turn_id,
                language=self._language,
                image_id=self._image_id,
                policy_fingerprint=self._policy_fingerprint,
                **fields,
            )
        )

    def output_preview(self, text: str) -> tuple[str, str, bool]:
        """A bounded preview plus a hash of the full text, for stdout/stderr.

        Reuses the existing ``tracing.preview_and_hash`` rather than inventing a
        second truncation rule, so a sandbox preview reads the same as a tool
        payload preview in the same file.
        """

        return preview_and_hash(text, limit=self._preview_chars)
