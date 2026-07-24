import json
import threading

import pytest

from tracing import (
    JsonlTraceSink,
    MemoryTraceSink,
    NullTraceSink,
    SafeTraceSink,
    build_event,
    preview_and_hash,
    utc_now_iso,
)


class TestBuildEvent:
    def test_required_fields_present(self):
        event = build_event("turn_started", run_id="r1", turn_id="t1")
        assert event["schema_version"] == 1
        assert event["event"] == "turn_started"
        assert event["run_id"] == "r1"
        assert event["turn_id"] == "t1"
        assert "timestamp" in event

    def test_timestamp_is_utc_and_timezone_aware(self):
        timestamp = utc_now_iso()
        assert timestamp.endswith("Z")
        # Must parse back as an aware datetime once the Z is normalized.
        from datetime import datetime

        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None


class TestPreviewAndHash:
    def test_short_value_is_not_truncated(self):
        preview, digest, truncated = preview_and_hash({"query": "SELECT 1"}, limit=1000)
        assert truncated is False
        assert preview == '{"query":"SELECT 1"}'
        assert len(digest) == 64

    def test_long_value_is_truncated_at_limit(self):
        value = {"query": "x" * 5000}
        preview, _, truncated = preview_and_hash(value, limit=100)
        assert truncated is True
        assert len(preview) == 100

    def test_hash_is_stable_for_identical_input(self):
        _, digest_a, _ = preview_and_hash({"a": 1, "b": 2}, limit=1000)
        _, digest_b, _ = preview_and_hash({"b": 2, "a": 1}, limit=1000)
        assert digest_a == digest_b

    def test_hash_differs_for_different_input(self):
        _, digest_a, _ = preview_and_hash({"query": "SELECT 1"}, limit=1000)
        _, digest_b, _ = preview_and_hash({"query": "SELECT 2"}, limit=1000)
        assert digest_a != digest_b


class TestNullAndMemorySinks:
    def test_null_sink_discards_silently(self):
        NullTraceSink().emit(build_event("turn_started", run_id="r1"))

    def test_memory_sink_records_in_order(self):
        sink = MemoryTraceSink()
        sink.emit(build_event("turn_started", run_id="r1"))
        sink.emit(build_event("turn_finished", run_id="r1"))
        assert [e["event"] for e in sink.events] == ["turn_started", "turn_finished"]


class TestJsonlTraceSink:
    def test_writes_one_valid_json_object_per_line(self, tmp_path):
        path = tmp_path / "traces" / "agent.jsonl"
        sink = JsonlTraceSink(path)
        sink.emit(build_event("turn_started", run_id="r1", turn_id="t1"))
        sink.emit(build_event("turn_finished", run_id="r1", turn_id="t1", status="completed"))

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["event"] == "turn_started"
        assert parsed[1]["event"] == "turn_finished"

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "agent.jsonl"
        JsonlTraceSink(path)
        assert path.parent.is_dir()

    def test_concurrent_writes_do_not_interleave_lines(self, tmp_path):
        path = tmp_path / "agent.jsonl"
        sink = JsonlTraceSink(path)

        def emit_many(worker_id: int) -> None:
            for i in range(50):
                sink.emit(build_event("turn_started", run_id=f"r-{worker_id}-{i}"))

        threads = [threading.Thread(target=emit_many, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 400
        for line in lines:
            json.loads(line)  # every line parses as exactly one JSON object


class TestSafeTraceSink:
    def test_delegates_to_inner_sink_when_healthy(self):
        inner = MemoryTraceSink()
        safe = SafeTraceSink(inner, run_id="r1")
        safe.emit(build_event("turn_started", run_id="r1"))
        assert len(inner.events) == 1

    def test_warns_once_and_does_not_raise_on_repeated_failures(self):
        class BrokenSink:
            def emit(self, event):
                raise OSError("disk full")

        warnings = []
        safe = SafeTraceSink(BrokenSink(), run_id="r1", on_warning=warnings.append)

        for _ in range(5):
            safe.emit(build_event("turn_started", run_id="r1"))

        assert len(warnings) == 1

    def test_trace_error_attempt_never_recurses_on_repeated_inner_failure(self):
        class AlwaysBroken:
            def emit(self, event):
                raise RuntimeError("still broken")

        safe = SafeTraceSink(AlwaysBroken(), run_id="r1", on_warning=lambda m: None)
        # Must not raise even though both the original emit and the
        # trace_error follow-up emit fail against the same broken sink.
        safe.emit(build_event("turn_started", run_id="r1"))
