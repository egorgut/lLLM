# SPEC-004 — Persistent JSON chat memory

- **Spec:** [SPEC-004](../../specs/SPEC-004-Persistent-JSON-Chat-Memory.md)
- **Date:** 2026-07-12
- **Branch:** feature/SPEC-004-persistent-json-chat-memory
- **Merge commit:** 98a8266

## Hypothesis / intent
Conversation history lived only in RAM, so restarts lost the dialogue. Persisting
history as JSON should let the chat survive restarts while keeping the layered,
framework-free design. The step also separates three responsibilities that were
previously blurred — **Conversation** (domain), **Conversation Storage** (JSON),
and **Model Context** (what the LLM sees) — so future summarization / token
budgeting / alternate backends can land without touching the CLI or LLM layers.

## What changed
- New `storage.py` with `JsonConversationStore`: `load` (missing → empty; invalid
  JSON → warn, preserve the file as `chat_history.corrupted-<UTC>.json`, start
  fresh) and `save` (creates `data/` lazily, writes the SPEC schema `version` /
  `conversation_id` / `updated_at` / `messages`, `ensure_ascii=False`).
- `conversation.py`: the system prompt is no longer stored inside the message
  list. `Conversation` holds only user/assistant messages (`_messages`), accepts
  preloaded history, and exposes two projections — `stored_messages` (full
  history, persisted) and `messages_for_model` (system prompt + last
  `MAX_CONTEXT_MESSAGES`).
- `config.py`: added `MAX_CONTEXT_MESSAGES = 20` and `CHAT_HISTORY_PATH`.
- `app.py`: loads history on startup, sends `messages_for_model` to the model,
  saves `stored_messages` after each successful exchange, and rewrites an empty
  conversation on `/reset`. LLM-failure rollback unchanged (no save on failure).
- `.gitignore`: `data/` → `data/*.json`; committed `data/.gitkeep`.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, ctx 40960, 8.2B)
- Ollama: 0.31.1
- Sampling: defaults — no `options` set in `llm.py`

## Verification
Drove the real app across separate processes and inspected the JSON store.

Scenario 1 — persistence across restart:
```
$ printf 'My project is called lLLM.\n/bye\n' | python app.py
You: Qwen: Your project is named lLLM. How can I assist you with it?
# (new process)
$ printf 'What is my project called?\n/bye\n' | python app.py
You: Qwen: Your project is called lLLM.
```
The stored file after turn 1 held only user/assistant messages (no system prompt),
with the schema `{version, conversation_id, updated_at, messages}`.

Scenario 2 — context window: constructed a 30-message history and checked the
projections — `stored_messages` = 30, `messages_for_model` = 21 (1 system + last
20, i.e. m10…m29). Full history is retained; the model sees only the window.

Scenario 3 — `/reset`:
```
$ printf 'Remember the number 42.\n/reset\n/bye\n' | python app.py
...
$ cat data/chat_history.json   →   "messages": []
```

Corruption path: wrote garbage into `chat_history.json`, restarted — app printed a
warning, renamed the bad file to `chat_history.corrupted-2026-07-12T17-52-55Z.json`,
and started a fresh, valid conversation.

## Outcome
All acceptance criteria met. History survives restarts, the system prompt is never
persisted, the model receives only the configured window, and corrupted files are
preserved rather than lost. Existing chat / `/reset` / `/bye` behavior unchanged.

## Follow-ups
- `save` rewrites the whole file each turn — fine at this scale; a future step
  could switch to append/atomic-write if histories get large.
- Single hardcoded `conversation_id "default"`; multiple chats remain out of scope.
