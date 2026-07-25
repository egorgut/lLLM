# PATCH-011-01 — Per-Session Trace File

## Parent spec

`specs/SPEC-011-Agent-Reliability-Observability.md`

## Problem

Every `python app.py` run currently appends to one shared, ever-growing file,
`data/traces/agent.jsonl` (`config.TRACE_PATH`). Correlating a single
session's events means filtering that whole file by `run_id`, and the file
never shrinks across the app's lifetime. `JsonlTraceSink`'s in-process lock
only serializes writes within one process — two `app.py` processes running
concurrently both append to the same path, relying on OS-level short-write
behavior rather than any guarantee this codebase makes.

## Expected change

Write each run's trace to its own file, `data/traces/agent-<run_id>.jsonl`,
instead of one shared file. The append-only JSONL format, event schema, and
every write guarantee in SPEC-011 §4 (append-only, no full-file rewrite,
resilient to an interrupted final line) are unchanged — only the file
granularity moves from "one file forever" to "one file per run".

## Constraints

- Preserve `JsonlTraceSink`'s existing behavior unchanged (append-only, one
  JSON object per line, no rewriting the file); it already accepts an
  arbitrary path, so it needs no changes itself.
- Do not change the event schema or `schema_version`.
- Do not change `SafeTraceSink`'s failure-handling behavior.
- Keep path construction in `tracing.py`, not scattered across `app.py`.
- Framework-free; no new dependency.

## Acceptance criteria

- Each `python app.py` run creates its own `data/traces/agent-<run_id>.jsonl`
  and only ever appends to that file.
- Two runs never write to the same trace file.
- `JsonlTraceSink` and `SafeTraceSink` behavior is unchanged (verified by the
  existing tests in `tests/test_tracing.py` continuing to pass unmodified).
- A regression test covers the new path-construction helper: directory +
  `run_id` in, `agent-<run_id>.jsonl` path out, distinct `run_id`s produce
  distinct paths.
- README's tracing section and config example reflect the new filename
  pattern and the renamed config constant.

## Files likely affected

- `tracing.py` — add a small path-construction helper.
- `config.py` — `TRACE_PATH` (single file) becomes `TRACE_DIR` (directory).
- `app.py` — build the per-run path from `TRACE_DIR` and the already-generated `run_id`.
- `tests/test_tracing.py` — regression test for the new helper.
- `README.md` — tracing section + config example.

This list is advisory, not restrictive.

## Verification

Deterministic code only; no model-facing behavior is touched, so no
live-model verification is required.

- `python -m pytest tests/test_tracing.py -q`
- `python -m pytest -q` (full suite, confirm no regression elsewhere)
- Manual sanity check: run `python app.py` twice in a row and confirm two
  distinct `data/traces/agent-*.jsonl` files exist, each containing only
  that run's events.

## Journal strategy

Append a `## Patches` subsection to the parent journal,
`docs/journal/SPEC-011-agent-reliability-observability.md` — deterministic
code only, no model-facing or model-decision impact.

## Out of scope

- Forwarding traces to an external observability system.
- Trace retention/cleanup policy (old per-run files are not pruned).
- Changing `schema_version` or any event's field set.
- Changing MCP or tool-call trace event content.
