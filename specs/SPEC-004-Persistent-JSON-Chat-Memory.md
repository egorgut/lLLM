# SPEC-004: Persistent JSON Chat Memory

## Background

The application currently stores conversation history only in RAM. When
the Python process exits, the dialogue is lost.

The purpose of this iteration is to introduce persistent chat history
while preserving the existing architecture and educational simplicity.

------------------------------------------------------------------------

# Goal

Implement persistent JSON-based chat memory.

The implementation must clearly separate:

-   Conversation (domain logic)
-   Conversation Storage (JSON persistence)
-   Model Context (messages sent to the LLM)

These are different responsibilities and must remain independent.

------------------------------------------------------------------------

# Architecture

Current:

``` text
CLI
 │
 ▼
Conversation
 │
 ▼
LLM Client
 │
 ▼
Ollama
```

Target:

``` text
CLI
 │
 ▼
Conversation
 │
 ├──────────────┐
 │              │
 ▼              ▼
LLM Client   JsonConversationStore
 │              │
 ▼              ▼
Ollama      chat_history.json
```

------------------------------------------------------------------------

# Design principles

## Stored history

Persist the complete conversation on disk.

Purpose:

-   persistence
-   debugging
-   future summarization
-   future retrieval

Stored history may grow indefinitely.

## Model context

Never send the full stored history to the model.

Instead send:

``` text
System Prompt
+
Last N messages
```

Add to `config.py`:

``` python
MAX_CONTEXT_MESSAGES = 20
```

## System prompt

The system prompt must NOT be stored in JSON.

It is always loaded from `prompts.py` and prepended before every model
invocation.

------------------------------------------------------------------------

# Functional requirements

## Storage component

Introduce a dedicated storage layer.

Suggested implementation:

``` text
storage.py
```

containing a class such as:

``` text
JsonConversationStore
```

Responsibilities:

-   load conversation
-   save conversation
-   create storage if missing
-   detect invalid JSON
-   preserve corrupted files

Conversation must not know how persistence works.

------------------------------------------------------------------------

## Storage location

``` text
data/chat_history.json
```

Commit:

``` text
data/.gitkeep
```

Add to `.gitignore`:

``` gitignore
data/*.json
```

------------------------------------------------------------------------

## JSON format

``` json
{
  "version": 1,
  "conversation_id": "default",
  "updated_at": "2026-07-12T15:30:00Z",
  "messages": [
    {
      "role": "user",
      "content": "Hello"
    },
    {
      "role": "assistant",
      "content": "Hi!"
    }
  ]
}
```

Do not store the system prompt.

------------------------------------------------------------------------

## Loading

Application startup:

1.  Try to load JSON.
2.  If missing → create empty conversation.
3.  If invalid → print warning, preserve corrupted file, start fresh
    conversation.

------------------------------------------------------------------------

## Saving

Persist after every successful user→assistant exchange.

If the LLM call fails, rollback the pending user message before saving.

------------------------------------------------------------------------

## Context projection

Conversation should expose two independent concepts.

Example:

``` python
stored_messages
```

returns the complete stored history.

Example:

``` python
messages_for_model
```

returns:

-   current system prompt
-   last MAX_CONTEXT_MESSAGES messages

------------------------------------------------------------------------

# Out of scope

Do NOT implement:

-   token counting
-   summarization
-   embeddings
-   vector databases
-   SQLite
-   long-term memory
-   multiple chats
-   RAG
-   MCP
-   tool calling

------------------------------------------------------------------------

# Acceptance criteria

Scenario 1:

    You: My project is called lLLM.
    /bye

Restart application.

    You: What is my project called?

Expected:

    lLLM

Scenario 2:

Conversation exceeds 20 messages.

Result:

-   JSON contains the full history.
-   The model receives only the configured context window.

Scenario 3:

    /reset

Conversation file is rewritten as an empty conversation.

------------------------------------------------------------------------

# Design rationale

This iteration intentionally separates:

Conversation

≠

Conversation Storage

≠

Model Context

This architectural boundary enables future implementation of:

-   context summarization
-   token budgeting
-   SQLite backend
-   semantic memory
-   cloud storage

without modifying the CLI or LLM layers.

------------------------------------------------------------------------

# Definition of Done

-   Conversation survives application restarts.
-   System prompt is never stored in JSON.
-   JSON contains only user/assistant messages.
-   Full history is persisted.
-   Model receives only the configured context window.
-   Runtime JSON files are ignored by Git.
-   Existing behaviour remains unchanged.
