# SPEC-005 — Streaming LLM responses

- **Spec:** [SPEC-005](../../specs/SPEC-005-Streaming-Responses.md)
- **Date:** 2026-07-13
- **Branch:** feature/SPEC-005-streaming-responses
- **Merge commit:** _(filled in after merge)_

## Hypothesis / intent
`chat_with_model()` blocked until Ollama finished generating, so the user stared
at a blank terminal for the whole generation. Streaming the response as it is
produced improves perceived responsiveness. The step keeps the existing layer
boundaries: `llm.py` produces text chunks (transport), `app.py` renders and
assembles them (presentation), and only the complete, successful assistant
message reaches `Conversation` and the JSON store. Chunks are never persisted.

## What changed
- `llm.py`: replaced `chat_with_model()` (returned `str`) with a generator
  `stream_chat_with_model(messages) -> Iterator[str]` that calls
  `client.chat(..., stream=True)` and yields each non-empty
  `chunk.message.content`. Validation (`if not messages`) is unchanged but, being
  in a generator body, now runs on first iteration rather than at call time — a
  one-line comment documents this. Dropped the `ChatResponse` import; the module
  still prints nothing and knows nothing about the CLI.
- `app.py`: prints the `Qwen: ` prefix, then loops over the generator printing
  each chunk with `flush=True` while accumulating into `response_parts`. After a
  successful stream it joins the parts, adds the assistant message, and saves.
  New failure/edge handling, each doing the same rollback via the existing
  `conversation.remove_last_message()` and `continue`: streaming exception,
  `KeyboardInterrupt` (prints "Generation interrupted."), and empty completed
  response (prints "Model returned an empty response." — reuses the error path,
  no custom exception).
- `conversation.py`, `storage.py`, `config.py`: unchanged. `messages_for_model`
  and `stored_messages` are reused as-is, so the system prompt and
  `MAX_CONTEXT_MESSAGES` window behave exactly as before. No JSON schema change;
  no `STREAMING_ENABLED` flag.
- `README.md`: one line in «Запуск» noting the reply is printed incrementally.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B)
- Ollama SDK: `ollama==0.6.2`; server reachable at `http://localhost:11434`
- Sampling: defaults — no `options` set in `llm.py`

## Verification
Drove the real app and exercised the generator directly.

Progressive output (AC-1/AC-2) — iterating the generator on "Count from 1 to 8"
produced **15 fragments** arriving over time, e.g.:
```
'1'
'  \n'
'2'
...
'8'
--- 15 fragments; first fragment at 5.57s; total 5.83s
```
The first *content* fragment arrived ~5.5s in: qwen3 does a server-side thinking
phase first, and thinking is surfaced separately (`message.thinking`), so
`message.content` is empty until reasoning completes — the content then streams
progressively. Whitespace-only fragments like `'  \n'` are non-empty and kept.

Persistence (AC-3/AC-4/AC-5) — `/reset` then "Say exactly: streaming works."
stored exactly one user + one assistant message, assistant content equal to the
visible reply, no chunk records, no system prompt:
```
"messages": [
  {"role": "user", "content": "Say exactly: streaming works."},
  {"role": "assistant", "content": "streaming works."}
]
```
Restarting and asking the model to quote its previous message returned
`"streaming works."`, confirming loaded history is used as context (AC-7).

Empty input (Scenario 4) — a blank line was skipped with no model request.

Failure rollback (Scenario 6 / AC-8) — pointed the client at an unreachable host
mid-run:
```
Qwen:
Application error: [Errno 61] Connection refused
```
The user message was rolled back and `data/chat_history.json` was byte-identical
before and after (`diff -q` → IDENTICAL).

Empty response (AC-9) — a stream yielding zero non-empty fragments printed
"Application error: Model returned an empty response.", rolled back the user
message, JSON unchanged.

Interruption (Scenario 5 / AC-8) — a `KeyboardInterrupt` after a partial fragment:
```
Qwen: The answer begins here
Generation interrupted.
```
Partial output stayed visible, the user message was rolled back, JSON unchanged,
and the input loop continued to `/bye`.

## Outcome
All acceptance criteria met. The reply streams progressively; only the complete,
successful response is added to `Conversation` and persisted; failed, empty, and
interrupted turns roll back the user message and leave the stored file untouched.
`llm.py` never prints, `app.py` never touches the Ollama SDK directly, and
`conversation.py` / `storage.py` remain unaware of streaming.

## Follow-ups (intentionally out of scope)
- Token-level metrics / tokens-per-second, timing dashboards.
- Rendering qwen3 thinking (`message.thinking`) as a distinct dimmed stream.
- Ollama-side cancellation, async streaming, retry logic — all SPEC-005 non-goals.
