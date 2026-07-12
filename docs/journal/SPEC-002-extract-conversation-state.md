# SPEC-002 — Extract conversation state from the CLI

- **Spec:** [SPEC-002](../../specs/SPEC-002-Extract-Conversation-State.md)
- **Date:** 2026-07-12
- **Branch:** feature/SPEC-002-conversation-state
- **Merge commit:** 5042c93

## Hypothesis / intent
`app.py` owned both the CLI loop and the raw `messages` list. Extracting history
into a dedicated `Conversation` should keep behavior identical while unblocking
future features (memory, trimming, summarization, RAG). Also add a `/reset`
command.

## What changed
- New `conversation.py` with a `Conversation` class owning the system prompt and
  message history: `add_user_message`, `add_assistant_message`,
  `remove_last_message` (rolls back on LLM error, never drops the system prompt),
  `reset`.
- `app.py` slimmed to CLI + command handling + delegation; no raw `messages`
  manipulation. Added `/reset`.
- README documents `/reset` and `conversation.py`.
- **In-branch follow-up (commit 6868e6c):** the model greeted on every turn
  because its first greeting sat in history and got echoed. Fixed at the
  system-prompt level (`prompts.py`): "this is an ongoing dialogue, do not open
  with a greeting." Strictly this was beyond SPEC-002's scope — going forward
  such a fix gets its own spec (see the spec-cycle convention).

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, ctx 40960)
- Ollama: 0.6.2
- Sampling: defaults — no `options` set in `llm.py`

## Verification
Drove the real app with a scripted dialogue:

```
$ printf 'My name is Egor.\nWhat is my name?\n/reset\nWhat is my name?\n/bye\n' | python app.py
You: Qwen: Egor, I'm your AI assistant in the lab. How can I assist you today?
You: Qwen: Your name is Egor.
You: Conversation cleared.
You: Qwen: I don't have information about your name. You haven't provided it...
```

- In-session memory works: "What is my name?" → "Your name is Egor."
- After `/reset` the model no longer remembers the name.
- After the prompt fix, no reply opens with "Hi/Hello/Привет" (before the fix
  the first reply was "Hello, Egor! Welcome back to the lab!").

## Outcome
Acceptance criteria met. Behavior preserved, `/reset` added, greeting regression
removed, memory intact.

## Follow-ups
- Model tag `qwen3:8b` is mutable; a future step could pin the digest in config
  for stronger reproducibility.
- Sampling parameters are implicit (model defaults); consider making them
  explicit when experiments start depending on them.
