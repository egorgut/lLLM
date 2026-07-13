# SPEC-005: Streaming LLM Responses

## Background

The application currently waits until Ollama generates the complete assistant response.

Only after generation is finished does `chat_with_model()` return a string to the CLI, which then prints the whole response at once.

Current interaction:

```text
User enters message
        │
        ▼
CLI calls chat_with_model()
        │
        ▼
Ollama generates complete response
        │
        ▼
chat_with_model() returns str
        │
        ▼
CLI prints complete response
```

This works functionally, but it creates a long period during which the user sees no output.

Modern AI applications improve perceived responsiveness by streaming generated content incrementally as it becomes available.

The purpose of this iteration is to introduce streaming while preserving the existing architectural boundaries.

---

## Goal

Implement streaming responses from Ollama to the CLI.

The assistant response must appear progressively in the terminal instead of being printed only after the full response has been generated.

At the same time, the application must still collect the complete assistant response so it can be added to `Conversation` and persisted after successful generation.

---

## Core architectural decision

Streaming introduces two distinct representations of one assistant response:

```text
Response chunks
```

and:

```text
Complete assistant message
```

These are not the same responsibility.

- The LLM layer produces response chunks.
- The CLI renders response chunks.
- The CLI assembles the chunks into one complete assistant message.
- `Conversation` receives only the complete assistant message.
- `JsonConversationStore` persists only completed messages.

The application must never store individual streaming chunks as separate conversation messages.

---

## Architecture

Current:

```text
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

```text
                       response chunks
                  ┌─────────────────────────┐
                  │                         ▼
CLI ───────► Conversation ───────► LLM Client ───────► Ollama
 │                                      ▲
 │                                      │
 └── renders chunks and assembles ──────┘
           complete response
                  │
                  ▼
           Conversation
                  │
                  ▼
      JsonConversationStore
```

A simpler runtime representation:

```text
Conversation messages
        │
        ▼
LLM streaming function
        │
        ▼
Iterator of text chunks
        │
        ▼
CLI prints each chunk immediately
        │
        ▼
CLI joins chunks
        │
        ▼
Complete assistant message
        │
        ├──► Conversation
        │
        └──► JSON Store
```

---

## Design principles

### 1. The LLM layer owns communication with Ollama

`llm.py` must remain responsible for:

- calling the Ollama SDK;
- enabling streaming;
- reading Ollama response events;
- extracting text content;
- exposing text chunks to the caller.

The LLM layer must not print anything to the terminal.

It must not know that the current interface is a CLI.

### 2. The CLI owns presentation

`app.py` must remain responsible for:

- printing the assistant prefix;
- printing each response chunk immediately;
- flushing terminal output;
- printing final line breaks;
- assembling the complete assistant response.

This keeps terminal-specific behavior outside the LLM client.

### 3. Conversation stores semantic messages, not transport events

`Conversation` must continue to store messages in this form:

```python
{
    "role": "assistant",
    "content": "Complete assistant response"
}
```

It must not store:

- raw Ollama response objects;
- individual chunks;
- token events;
- generator objects;
- partial messages.

No changes to the persistent JSON schema are required.

### 4. Persistence occurs only after successful completion

The assistant response must be added to `Conversation` and saved to JSON only after the stream finishes successfully.

During generation:

```text
chunks printed to terminal
```

but:

```text
assistant message not yet persisted
```

After successful stream completion:

```text
complete response
    │
    ▼
Conversation.add_assistant_message(...)
    │
    ▼
store.save(...)
```

This prevents incomplete responses from silently becoming valid conversation history.

### 5. Do not introduce an agent framework

This iteration must use only:

- Python;
- the official `ollama` Python SDK;
- the existing application layers.

Do not introduce:

- LangChain;
- LangGraph;
- AutoGen;
- CrewAI;
- asynchronous frameworks;
- terminal UI frameworks;
- event buses;
- callback frameworks.

The goal is to understand the streaming mechanism directly.

---

## Functional requirements

### Streaming LLM function

Change the LLM interface so that it produces text chunks rather than returning one complete string.

Suggested public interface:

```python
def stream_chat_with_model(
    messages: list[dict[str, str]],
) -> Iterator[str]:
    ...
```

A generator is preferred because it naturally represents incremental output.

The exact type may use `Iterator[str]` or `Generator[str, None, None]`.

`Iterator[str]` is preferred for the public return annotation because callers only need to iterate over it.

### Ollama streaming mode

Use the official Ollama client with streaming enabled:

```python
client.chat(
    model=MODEL_NAME,
    messages=messages,
    stream=True,
)
```

The Ollama SDK returns an iterable stream of response chunks.

For each Ollama response event:

1. Extract the assistant content fragment.
2. Ignore empty fragments.
3. Yield non-empty text fragments to the caller.

Conceptually:

```python
for chunk in response_stream:
    content = chunk.message.content

    if content:
        yield content
```

Use the actual response types provided by the installed Ollama SDK version.

Do not expose Ollama response objects outside `llm.py`.

### Message validation

Preserve the current validation:

```python
if not messages:
    raise ValueError("Message history cannot be empty.")
```

The validation must happen when the streaming function is actually iterated.

Because generator function bodies do not execute when the generator object is created, this behavior should be understood and documented if relevant.

Do not add unnecessary validation abstractions in this iteration.

### CLI rendering

Replace the current full-response flow:

```python
assistant_message = chat_with_model(...)
print(f"\nQwen: {assistant_message}\n")
```

with incremental rendering.

Expected terminal behavior:

```text
You: Explain streaming simply.

Qwen: Streaming means that the model sends its answer...
```

The prefix:

```text
Qwen:
```

must be printed before the first chunk.

Each chunk must be printed using:

```python
print(chunk, end="", flush=True)
```

or equivalent behavior.

`flush=True` is required so content appears immediately instead of waiting in the output buffer.

After generation completes, print the necessary final newline characters so the next `You:` prompt is formatted correctly.

### Response assembly

While printing chunks, `app.py` must also collect them.

Suggested flow:

```python
response_parts: list[str] = []

for chunk in stream_chat_with_model(...):
    print(chunk, end="", flush=True)
    response_parts.append(chunk)

assistant_message = "".join(response_parts)
```

The assembled string is the semantic assistant message.

Only this complete string may be passed to:

```python
conversation.add_assistant_message(assistant_message)
```

### Successful exchange

A successful turn must follow this sequence:

```text
1. Read user input
2. Add user message to Conversation
3. Request streaming response
4. Print chunks as they arrive
5. Assemble complete assistant response
6. Add complete assistant response to Conversation
7. Persist complete conversation
```

The order is important.

Persistence must happen only after step 6.

### Empty completed response

If the stream completes without producing any non-empty content, treat the model call as unsuccessful.

Do not add an empty assistant message to `Conversation`.

Do not persist the user message as part of a completed exchange.

Raise or handle an application-level error such as:

```text
Model returned an empty response.
```

Use a simple implementation. Do not introduce custom exception hierarchies in this iteration.

---

## Error handling

Streaming creates a new failure case: an exception may occur after part of the response has already been printed.

Example:

```text
Qwen: The answer begins here, but then...
Application error: connection closed
```

The terminal output cannot be “unprinted,” but the conversation state can remain consistent.

### Required behavior on streaming failure

If an exception occurs at any point before stream completion:

1. Print a newline so the error message does not continue on the same line as partial output.
2. Print the application error.
3. Remove the user message that was added before the model call.
4. Do not add any assistant message.
5. Do not save the failed exchange to JSON.
6. Return to the input loop.

The existing rollback principle must remain:

```python
conversation.remove_last_message()
```

The stored conversation should therefore remain exactly as it was before the failed turn.

### Partial output policy

A partial response may have been visible to the user before failure.

That partial output must not be treated as a valid assistant message.

The architecture must distinguish:

```text
visible terminal output
```

from:

```text
committed conversation state
```

Only successfully completed responses are committed.

### Keyboard interruption

Handle `KeyboardInterrupt` during active streaming gracefully.

Preferred behavior:

1. Move to a new terminal line.
2. Print a short message such as:

```text
Generation interrupted.
```

3. Roll back the current user message.
4. Do not persist the partial response.
5. Return to the normal chat input loop.

Do not terminate the entire application unless the interruption occurs while waiting for user input and the existing application behavior naturally exits.

Keep this handling local and simple.

---

## Files to modify

### `llm.py`

Responsibilities after this iteration:

- configure the Ollama client;
- validate input messages;
- call Ollama with `stream=True`;
- translate Ollama response objects into text chunks;
- yield text chunks;
- never print terminal output.

Rename or replace:

```python
chat_with_model(...)
```

with:

```python
stream_chat_with_model(...)
```

There is no requirement to preserve the old non-streaming function unless it is still genuinely useful.

Avoid maintaining two interfaces without a current use case.

### `app.py`

Responsibilities after this iteration:

- call the streaming LLM function;
- print the assistant prefix;
- print and flush chunks;
- assemble the complete response;
- commit the assistant response after successful completion;
- preserve rollback behavior on failure;
- persist only successful exchanges.

The CLI must not access the Ollama SDK directly.

### `conversation.py`

No architectural changes should be required.

`Conversation` must continue to receive only complete user and assistant messages.

Do not move streaming logic into `Conversation`.

### `storage.py`

No changes should be required.

The JSON format and persistence rules remain unchanged.

Do not store partial responses.

### `config.py`

No new configuration is required for basic streaming.

Do not add a `STREAMING_ENABLED` feature flag in this iteration.

Streaming becomes the normal response mode.

### `README.md`

Update the usage description to mention that assistant responses are printed incrementally.

Do not over-document Ollama internals in the README.

A concise statement is sufficient.

---

## Public interface

Expected LLM interface after implementation:

```python
from collections.abc import Iterator


def stream_chat_with_model(
    messages: list[dict[str, str]],
) -> Iterator[str]:
    """Yield assistant response text as it is generated by Ollama."""
```

Expected caller behavior:

```python
response_parts: list[str] = []

for chunk in stream_chat_with_model(conversation.messages_for_model):
    print(chunk, end="", flush=True)
    response_parts.append(chunk)

assistant_message = "".join(response_parts)
```

This code is illustrative, not mandatory line-for-line implementation.

---

## Non-goals

The following are explicitly outside the scope of SPEC-005:

- token-level metrics;
- tokens-per-second calculation;
- timing or performance dashboards;
- cancellation through Ollama APIs;
- asynchronous streaming;
- concurrent model requests;
- multiple simultaneous conversations;
- WebSocket or HTTP APIs;
- graphical interfaces;
- Markdown-aware terminal rendering;
- Rich/Textual integration;
- streaming tool calls;
- streaming reasoning events;
- storing partial responses;
- resuming interrupted generations;
- retry logic;
- agent loops;
- tests with mocked LLM frameworks.

These may be introduced in later iterations when there is a concrete architectural need.

---

## Acceptance criteria

### AC-1: Progressive output

Given a normal user message, the assistant response appears progressively in the terminal while Ollama is generating it.

The application does not wait for the complete response before displaying content.

### AC-2: Immediate flushing

Each non-empty content chunk is written immediately.

Output buffering must not cause the response to appear all at once at the end.

### AC-3: Complete conversation message

After successful generation, all chunks are joined into one complete string.

`Conversation` receives exactly one assistant message for the turn.

### AC-4: Persistent complete response

After successful generation, the complete assistant message is saved to `data/chat_history.json`.

The persisted content must equal the complete visible generated response.

### AC-5: No chunk persistence

Individual chunks are not stored as separate messages or separate JSON entries.

The JSON schema remains unchanged.

### AC-6: System prompt behavior unchanged

The system prompt continues to come from `prompts.py`.

It is sent to the model through `messages_for_model`.

It is never persisted to JSON.

### AC-7: Context window behavior unchanged

The model continues to receive:

```text
system prompt
+
last MAX_CONTEXT_MESSAGES stored messages
```

Streaming must not change context-window behavior.

### AC-8: Failure rollback

If streaming fails before completion:

- any partial terminal output may remain visible;
- no assistant message is added;
- the latest user message is removed;
- the JSON file is not updated with the failed exchange.

### AC-9: Empty response handling

If no non-empty chunks are produced:

- no empty assistant message is added;
- the user message is rolled back;
- the exchange is not persisted;
- the CLI displays an error.

### AC-10: Reset and exit unchanged

`/reset` continues to clear and persist an empty conversation.

`/bye` continues to exit the application.

Streaming must not alter command handling.

### AC-11: Architectural boundaries

- `llm.py` does not print.
- `app.py` does not call the Ollama SDK directly.
- `conversation.py` does not process chunks.
- `storage.py` does not know about streaming.

---

## Manual verification scenarios

### Scenario 1: Normal streaming

Start the application:

```bash
python app.py
```

Enter a prompt that produces a reasonably long answer:

```text
Explain how a local LLM harness works in five paragraphs.
```

Expected:

- `Qwen:` appears before the generated answer;
- text appears progressively;
- the terminal remains responsive;
- the next `You:` prompt appears on a properly formatted new line.

### Scenario 2: Persistence

After a successful response, inspect:

```bash
cat data/chat_history.json
```

Expected:

- one user message for the prompt;
- one assistant message containing the complete response;
- no individual chunk records;
- no system prompt.

Restart the app and ask a follow-up question.

Expected:

- the previous complete response is available as conversation context.

### Scenario 3: Reset

Run:

```text
/reset
```

Expected:

- in-memory conversation is empty;
- JSON contains an empty `messages` array;
- subsequent streaming still works normally.

### Scenario 4: Empty input

Press Enter without entering a message.

Expected:

- no model request;
- no output corruption;
- CLI asks for input again.

### Scenario 5: Stream interruption

While a long answer is being generated, interrupt generation with `Ctrl+C`.

Expected:

- output moves to a new line;
- a short interruption message is printed;
- the current user message is removed from `Conversation`;
- no partial assistant response is saved;
- the application remains usable.

Then ask another question to verify the loop continues.

### Scenario 6: Ollama failure

Stop Ollama or otherwise force the stream to fail.

Expected:

- application prints an error;
- current user message is rolled back;
- no assistant message is persisted;
- prior conversation history remains intact.

Restart Ollama and verify that a new turn works.

---

## Suggested implementation sequence

1. Replace the non-streaming function in `llm.py` with a generator-based streaming function.
2. Confirm manually that iterating over it yields multiple content fragments.
3. Update `app.py` to print and flush each fragment.
4. Accumulate fragments into a complete assistant message.
5. Add and persist the assistant message only after successful completion.
6. Add handling for empty streams.
7. Preserve rollback behavior for exceptions.
8. Handle `KeyboardInterrupt` during generation.
9. Update README.
10. Run all manual verification scenarios.
11. Record the result in the iteration journal.
12. Commit the iteration as one architectural step.

---

## Suggested branch

```text
feature/SPEC-005-streaming-responses
```

## Suggested spec file

```text
specs/SPEC-005-Streaming-Responses.md
```

## Suggested journal file

```text
docs/journal/SPEC-005-Streaming-Responses.md
```

## Suggested commit message

```text
Add streaming LLM responses (SPEC-005)
```

---

## Expected journal content

The journal entry should record:

- the implementation summary;
- Ollama version;
- model name and digest;
- whether default sampling parameters were used;
- examples showing progressive output;
- verification of persisted complete response;
- interruption behavior;
- failure rollback behavior;
- any unexpected Ollama SDK chunk behavior;
- final outcome;
- follow-up ideas intentionally left outside this iteration.

---

## Definition of done

SPEC-005 is complete when:

1. Ollama is called in streaming mode.
2. The LLM layer exposes response text as an iterator of chunks.
3. The CLI prints and flushes chunks incrementally.
4. The CLI assembles a complete assistant response.
5. Only the complete response is added to `Conversation`.
6. Only successful exchanges are persisted.
7. Failed or interrupted streams roll back the user message.
8. Existing context, reset, persistence, and exit behavior still work.
9. Architectural responsibilities remain separated.
10. Manual verification is documented in the iteration journal.
