# SPEC-002: Extract Conversation State from CLI

## Background

Current application architecture:

``` text
User
  │
  ▼
app.py
  │
  ▼
llm.py
  │
  ▼
Ollama
  │
  ▼
Qwen
```

`app.py` currently has two responsibilities:

1.  CLI interaction.
2.  Conversation state management.

Conversation history (`messages`) is created, modified and owned by
`app.py`.

Although this works correctly, it violates separation of concerns and
will make future features (persistent memory, summarization, context
trimming, RAG, tools) harder to implement.

The next iteration should introduce a dedicated conversation component
responsible only for chat history management.

------------------------------------------------------------------------

# Goal

Extract conversation state management from `app.py` into a dedicated
module without changing the application's observable behavior.

The user should not notice any behavioral differences except one new
command:

``` text
/reset
```

which clears the current conversation.

------------------------------------------------------------------------

# High-level architecture

## Current

``` text
app.py
 ├── CLI
 ├── messages
 └── calls llm.py
```

## Target

``` text
app.py
    │
    ▼
Conversation
    │
    ▼
llm.py
    │
    ▼
Ollama
```

The `Conversation` object becomes the owner of all dialogue history.

------------------------------------------------------------------------

# Functional requirements

## 1. Create new module

Create:

``` text
conversation.py
```

containing a `Conversation` class.

------------------------------------------------------------------------

## 2. Responsibilities

Conversation owns:

-   system prompt
-   user messages
-   assistant messages

Suggested public interface:

``` python
Conversation()

conversation.messages

conversation.add_user_message(...)

conversation.add_assistant_message(...)

conversation.reset()

conversation.remove_last_message()
```

Implementation details are up to the developer.

------------------------------------------------------------------------

## 3. app.py responsibilities

After refactoring, `app.py` should only:

-   read user input
-   recognize commands
-   call `Conversation` methods
-   call `chat_with_model()`
-   print responses

`app.py` must not manipulate the raw `messages` list directly.

------------------------------------------------------------------------

## 4. New CLI command

Support:

``` text
/reset
```

Behavior:

-   conversation history is cleared
-   system prompt remains
-   next question starts a fresh dialogue

------------------------------------------------------------------------

## 5. Error handling

If the LLM call fails after the user message has already been appended,
`Conversation` should roll back the last user message so that history
remains consistent.

------------------------------------------------------------------------

# Non-functional requirements

The implementation should:

-   remain framework-free
-   avoid LangChain, LangGraph, CrewAI, etc.
-   keep the code simple and educational
-   prioritize readability over abstraction

------------------------------------------------------------------------

# Out of scope

Do **not** introduce:

-   persistent memory
-   JSON serialization
-   SQLite
-   embeddings
-   vector databases
-   RAG
-   tools
-   MCP
-   streaming responses

These belong to future iterations.

------------------------------------------------------------------------

# Acceptance criteria

The dialogue:

``` text
You: My name is Egor.
You: What is my name?
```

must still return:

``` text
Egor
```

After:

``` text
/reset
```

the dialogue:

``` text
What is my name?
```

must no longer remember previous context.

------------------------------------------------------------------------

# Expected project structure

``` text
project/

app.py
conversation.py
llm.py
config.py
prompts.py
tools/
```

------------------------------------------------------------------------

# Design rationale

The purpose of this refactoring is **not** adding functionality.

Its purpose is introducing a dedicated `Conversation` abstraction that
will later evolve into:

-   persistent memory
-   context trimming
-   conversation summarization
-   long-term memory
-   RAG integration

without requiring changes to the CLI or LLM layers.

------------------------------------------------------------------------

# Definition of Done

-   Existing functionality preserved.
-   `/reset` implemented.
-   Conversation logic fully extracted from `app.py`.
-   No direct manipulation of raw message lists outside
    `conversation.py`.
-   README updated if necessary.
