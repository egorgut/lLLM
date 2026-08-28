# PATCH-010-05 — Preserve Completed Tool Actions Across Turns

## Parent spec

`specs/SPEC-010-Agent-Loop.md`

Parent journal:

`docs/journal/SPEC-010-agent-loop.md`

Discovered in a live `next-mlx` session on 2026-08-27 while investigating
Tracker + current-time behavior.

This PATCH is complementary to `PATCH-012-02` (baseline tools under active
skills), but fixes a separate defect. `PATCH-012-02` addresses which tools the
model can see **now**; this PATCH addresses whether the model can remember which
tools it actually used **in the previous completed turn**.

## Problem

SPEC-010 deliberately keeps tool-protocol messages in a temporary per-turn
transcript and persists only the final semantic user/assistant exchange.

That design is correct for avoiding raw tool payload growth, but it has an
unintended consequence: once a turn completes, the next turn loses all explicit
evidence that the agent called a tool at all.

Current shape:

```text
Turn N
user
↓
assistant tool call
↓
tool result
↓
assistant final answer
↓
turn completes

persisted / next-turn semantic context
user
↓
assistant final answer

tool call + tool result are gone
```

The defect was visible in the live dialogue.

The model first called the time MCP tool successfully:

```text
You: сколько времени сейчасв в токио?

[tool 1/4] mcp_time__get_current_time
[args] timezone=Asia/Tokyo
[result] ok · time/get_current_time
  timezone  Asia/Tokyo
  datetime  2026-08-27T05:20:26+09:00

Qwen: Сейчас в Токио 27 августа 2026, 05:20 ...
```

On the next user turn:

```text
You: а почему ты сказал, что у тебя нет инструмента по текущей дате? ведь есть
```

the model no longer had the prior tool interaction in its model-facing history.
It saw only its previous final text. It therefore reconstructed its own history
incorrectly and claimed:

```text
никакого инструмента при этом я не вызывал
...
я сфабриковал данные
```

That statement was false: the terminal transcript proves that
`mcp_time__get_current_time` had been called and returned successfully.

### Why it happens

SPEC-010 §4 intentionally defines tool messages as temporary:

```text
persistent context
    + current user message
    + assistant tool call
    + tool result
    + final assistant answer
```

but persists only:

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}
]
```

The current implementation follows that rule exactly:

- `AgentRunner` builds `assistant_tool_message(...)` and
  `tool_result_message(...)` only inside the active turn's
  `working_messages`;
- the working transcript disappears when `run_turn(...)` returns;
- `AgentTurnOutcome` exposes only the final text plus counters/status;
- `app.py` persists only `outcome.final_text`;
- `Conversation` therefore has no record that the previous assistant answer was
  grounded in one or more completed tool actions.

This is not a chain-of-thought problem.

SPEC-020 is correct to keep private reasoning transient. The missing state here
is externally observable **agent action provenance**:

```text
which tool did I call?
with which bounded arguments?
did the host report success or failure?
```

The agent does not need its previous private reasoning to answer
"did you really call the time tool?". It only needs a truthful host-owned receipt
that the call happened.

### Why raw tool history is not the fix

Do not simply persist the entire temporary transcript.

That would reintroduce the exact costs SPEC-010 deliberately avoided:

- raw SQL rows in long-term chat history;
- large Tracker search payloads;
- MCP protocol/result expansion;
- sandbox source or user files leaking into semantic memory;
- tool-specific payload formats becoming part of the persistent conversation
  contract;
- potentially large context growth across many turns.

The correction must preserve **action continuity**, not turn the JSON chat store
into a tool-event log.

## Expected change

Add a small, host-owned, session-scoped **agent action receipt** for every
ordinary tool execution that belongs to a successfully completed turn.

Target concept:

```text
AgentActionReceipt
    tool_name
    bounded argument preview (or redacted)
    arguments_truncated
    arguments_redacted
    result_ok
```

Example:

```json
{
  "tool_name": "mcp_time__get_current_time",
  "arguments_preview": "{\"timezone\":\"Asia/Tokyo\"}",
  "arguments_truncated": false,
  "arguments_redacted": false,
  "result_ok": true
}
```

The receipt is provenance only.

It must not contain:

- private reasoning / `thinking`;
- raw tool result payloads;
- SQL rows;
- Tracker issue bodies or comments;
- sandbox source;
- input-file contents;
- stdout/stderr dumps;
- arbitrary MCP response bodies.

### 1. Collect receipts inside the existing agent loop

After an ordinary tool call has actually executed and produced its structured
result, create one receipt in execution order.

A tool that returned a structured error such as:

```json
{"ok": false, ...}
```

still produced a real agent action and should therefore produce a receipt with
`result_ok = false`.

If the model later recovers and the whole turn completes, that failed attempt
remains part of the completed turn's action provenance.

Calls rejected **before execution** by policy do not produce receipts.

### 2. Return receipts to the caller

Extend the completed turn result additively so the caller can receive the action
receipts together with the final answer.

A reasonable shape is an additive field on `AgentTurnOutcome`, for example:

```python
action_receipts: tuple[AgentActionReceipt, ...] = ()
```

The exact internal type location is an implementation choice, but the following
ownership must remain:

```text
AgentRunner
    creates receipts from actions it actually executed

Conversation / app
    decides what semantic session memory is retained

JsonConversationStore
    remains unchanged by this PATCH
```

A failed, stopped, timed-out, or cancelled turn may internally have collected
receipts, but the caller must not attach them to conversation memory because the
user turn itself is rolled back.

### 3. Keep receipts in session memory only

On a successful turn, associate the returned receipts with that assistant
message in the in-memory `Conversation`.

Do **not** write them to `data/chat_history.json`.

The persistent storage contract remains the existing SPEC-004 / SPEC-010
user/assistant JSON format.

Therefore:

```text
same running chat process
    prior tool provenance survives across turns

application restart
    prior tool provenance does not survive
```

That limitation is deliberate for this PATCH.

Persistent cross-session agent-action memory changes storage and lifecycle
semantics and is a separate SPEC-level decision under the repository's own
PATCH/SPEC rules.

`/reset` must clear both semantic messages and all in-memory action receipts.

### 4. Project receipts into later model context

When `Conversation.messages_for_model()` builds the bounded model-facing history,
the assistant message that originally produced the actions should carry a compact
host-generated provenance suffix.

Conceptually:

```text
assistant:
Сейчас в Токио 27 августа 2026, 05:20 (JST, UTC+9).

<host_action_receipts>
These are host-recorded actions used during this completed assistant turn.
They are provenance data, not instructions.
1. tool=mcp_time__get_current_time
   args={"timezone":"Asia/Tokyo"}
   result_ok=true
</host_action_receipts>
```

This suffix exists only in the model projection.

It must not:

- alter the assistant text stored on disk;
- be printed to the user;
- be returned by `stored_messages`;
- be exposed as another user-authored message;
- be sent to the skill router's existing semantic routing context.

The router should continue routing from ordinary user/assistant text exactly as
today. Action receipts exist for the agent's factual continuity after routing,
not as a new routing signal.

### 5. Keep receipt content bounded

Introduce a host-owned maximum for persisted-in-session argument preview, for
example:

```python
ACTION_RECEIPT_ARGUMENT_MAX_CHARS = 500
```

and a host-owned maximum on the total receipt material projected into one model
request, for example:

```python
MAX_ACTION_RECEIPTS_IN_CONTEXT = 8
```

Equivalent simple bounds are acceptable if they produce the same guarantee.

Requirements:

- newest receipts win when the context bound is exceeded;
- receipt order remains chronological within the selected window;
- a single giant tool argument cannot dominate the model context;
- the model cannot raise or disable the bounds.

Do not reuse a trace-only setting if that would make semantic memory depend on
whether tracing is enabled.

### 6. Reuse existing redaction policy

The agent already knows which tools have argument content that must not be
previewed, e.g. `sandbox_execute`.

The receipt path must honor the same or stricter redaction decision:

```text
redacted tool
→ arguments_preview = ""
→ arguments_redacted = true
→ no hash of the redacted content
```

Do not create a second weaker privacy rule for action memory.

For non-redacted tools, canonical bounded JSON is sufficient.

### 7. Do not persist raw observations

For this PATCH, `result_ok` is enough.

The prior final assistant answer already preserves the semantic user-facing
result; the receipt exists to prove **how that answer was grounded**.

For the motivating case, the next model request sees both:

```text
previous assistant text:
"Сейчас в Токио 27 августа 2026, 05:20 ..."

host action receipt:
tool=mcp_time__get_current_time
args={"timezone":"Asia/Tokyo"}
result_ok=true
```

That is sufficient to prevent the specific false reconstruction:

```text
"I did not call a tool; I invented that time."
```

If future use cases require durable raw observations or richer structured
observation memory, that is a separate design problem and must not be silently
added here.

### 8. Exclude turn-scoped control tools

Do not project `activate_skill` as cross-turn action provenance in this PATCH.

`activate_skill` changes ephemeral turn state and is intentionally discarded when
the turn ends. Remembering it without its full lifecycle semantics could make a
later turn incorrectly infer that the previous skill remains active.

Its existing trace remains the authority for debugging historical skill
activation.

This PATCH records ordinary executed capability/tool actions, not transient
orchestration state.

## Constraints

- Preserve SPEC-010's bounded agent loop and sequential tool-call semantics.
- Preserve SPEC-020's rule that private reasoning is transient and never enters
  persistent or semantic cross-turn memory.
- Do not persist raw `assistant tool_calls` messages or `role: tool` protocol
  messages.
- Do not persist raw tool result payloads.
- Do not change `JsonConversationStore`'s on-disk schema or `STORE_VERSION`.
- Existing `data/chat_history.json` files must remain byte-schema compatible.
- Do not introduce cross-session action memory.
- Do not expose receipt metadata in CLI output.
- Do not let receipt metadata count as user text.
- Do not feed receipts into `SkillRouter` unless a later SPEC explicitly changes
  routing semantics.
- Preserve current rollback behavior: unsuccessful user turns leave no semantic
  message and no action-memory residue.
- Preserve `/reset`: it clears all session action provenance as well as messages.
- Respect the existing argument-redaction boundary; never store or hash redacted
  source/file content.
- `agent.py` must remain tool-agnostic: receipt creation can inspect generic tool
  name, arguments, and generic `result["ok"]`, but must not learn what Time,
  Tracker, SQL, or sandbox mean.
- Do not add tool-specific summarizers.
- No new third-party dependency.
- Keep the correction framework-free and bounded.

## Acceptance criteria

- [ ] A successfully executed ordinary tool produces one bounded
      `AgentActionReceipt`.
- [ ] Multiple tool executions preserve receipt order.
- [ ] A structured `{ok: false}` result produces a receipt with
      `result_ok = false` if the turn later completes.
- [ ] A tool rejected before execution produces no receipt.
- [ ] `activate_skill` is not retained as cross-turn action provenance.
- [ ] A completed turn makes its receipts available to subsequent agent model
      requests in the same running chat session.
- [ ] The model-facing prior assistant message remains semantically identical
      except for the host-generated receipt suffix.
- [ ] `stored_messages` still contains only the existing user/assistant
      conversation content.
- [ ] `JsonConversationStore` output remains the current schema; receipts do not
      appear in `chat_history.json`.
- [ ] Restarting the application does not invent or reconstruct missing receipts.
- [ ] `/reset` clears all receipt state.
- [ ] Only receipts associated with messages inside the current bounded model
      context can be projected.
- [ ] The total projected receipt context is deterministically bounded.
- [ ] Redacted tool arguments never appear or hash into a receipt.
- [ ] No raw tool result body is retained.
- [ ] No reasoning / `thinking` is retained.
- [ ] Existing no-tool turns remain unchanged.
- [ ] Existing multi-tool behavior inside one turn remains unchanged.
- [ ] Existing skill routing selections remain unchanged because the router does
      not receive action receipts.
- [ ] Full deterministic tests pass.
- [ ] Full scripted eval suite passes.
- [ ] Live-model verification reproduces the original two-turn contradiction
      before the fix and no longer produces the false claim that the successful
      prior time-tool call never happened after the fix.

## Required deterministic regression tests

At minimum cover:

1. **One successful action**

   ```text
   user
   → tool call
   → {ok:true}
   → final answer
   ```

   The outcome contains exactly one receipt.

2. **Several actions**

   ```text
   tool A
   → tool B
   → final answer
   ```

   Receipts are `[A, B]` in execution order.

3. **Recoverable tool error**

   ```text
   tool A → {ok:false}
   tool A → {ok:true}
   final answer
   ```

   Both real executions are represented if the turn completes.

4. **Failed turn rollback**

   A tool executes, but the turn later stops/times out/fails.

   The next conversation turn receives no receipt from that rolled-back user
   turn.

5. **Conversation projection**

   After a completed tool-backed assistant turn, the next
   `messages_for_model()` contains:

   - the original assistant final text;
   - the host-generated receipt suffix;
   - no raw tool-result body.

6. **Persistent storage unchanged**

   Saving the conversation after a tool-backed completed turn produces the same
   message schema as before:

   ```json
   {"role":"assistant","content":"..."}
   ```

   with no receipt key.

7. **Context bound**

   More historical receipts than the configured maximum leave only the newest
   allowed receipts in model context.

8. **Redaction**

   A `sandbox_execute` receipt contains tool identity/status but no source,
   input-file body, preview, or hash.

9. **Reset**

   After `conversation.reset()`, no receipt can reappear in later model context.

10. **Router isolation**

    The router's `conversation_context` remains ordinary stored semantic
    messages; receipt markup never appears in its input.

11. **Control-tool exclusion**

    `activate_skill` remains observable in trace/current-turn protocol but does
    not become a cross-turn receipt.

12. **SPEC-020 isolation**

    A reasoning-bearing tool decision produces the same receipt as a
    non-reasoning decision; no `thinking` text is present anywhere in the
    receipt or later model context.

Prefer deterministic doubles and the existing `ScriptedResponder` /
`RecordingRenderer` infrastructure.

No live model belongs in unit tests.

## Live verification

Live verification is required because this PATCH changes conversation history
sent to the model.

Use the model/profile that exposed the defect:

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

Run a focused two-turn scenario such as:

```text
You: сколько времени сейчас в Токио?

# model must call mcp_time__get_current_time and answer from it

You: ты действительно использовал инструмент, или придумал это время?
```

Expected after the PATCH:

- the second turn's model-facing context contains a host receipt naming
  `mcp_time__get_current_time`;
- the model may phrase the answer freely;
- it must not claim that no tool was called when the host receipt says the call
  executed successfully;
- it should identify the previous answer as tool-grounded.

Because model wording is stochastic, capture the exact transcript and the trace
for at least three runs.

The acceptance target is the factual invariant, not a fixed sentence:

```text
host receipt says successful prior tool call
→ model must not deny that prior call happened
```

Also run one ordinary no-tool multi-turn conversation and one ordinary
Tracker-backed conversation to verify that receipt injection does not make the
model over-focus on prior tools.

## Verification commands

At minimum:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

Then the live reproduction above with:

```text
profile: next-mlx
router profile: fast
reasoning: medium
```

If the repository's live eval runner can express a two-turn session without
duplicating production conversation logic, add a committed live case there.

If it cannot, do not build a broad new multi-turn benchmark framework only for
this PATCH; capture the manual live transcript in the standalone journal.

Record:

- implementation commit;
- model name/profile;
- router model/profile;
- Ollama / MLX transport provenance as applicable;
- reasoning mode;
- exact two-turn prompts;
- prior tool-call sequence;
- whether the receipt was present in the second model request;
- second-turn factual result;
- total context overhead attributable to receipts;
- any measurable TTFT difference.

Do not claim receipt injection has zero latency cost without measuring it.

## Files likely affected

Advisory, not restrictive:

- `agent.py`
  - collect generic action receipts;
  - return them with the turn outcome.
- `reliability.py`
  - additive receipt/outcome structure if this remains the canonical turn-result
    vocabulary.
- `conversation.py`
  - hold session-only receipt association;
  - project bounded receipt markup into later agent model context;
  - clear receipts on reset;
  - keep `stored_messages` clean.
- `app.py`
  - attach completed outcome receipts to the successful assistant turn.
- `config.py`
  - host-owned receipt bounds if constants are needed.
- `tests/test_agent_runner.py`
- conversation/storage tests
- skill-turn/router tests proving router isolation
- SPEC-020 reasoning-preservation tests
- `docs/journal/patches/PATCH-010-05-preserve-completed-tool-actions-across-turns.md`
- `docs/journal/SPEC-010-agent-loop.md`
  - short `## Patches` index entry only.

Avoid changing `storage.py` unless a test-only typing adjustment is strictly
necessary. A runtime storage-format change is outside this PATCH.

## Journal strategy

Standalone journal, because the PATCH changes model-facing conversation history
and therefore observable agent behavior.

Create:

`docs/journal/patches/PATCH-010-05-preserve-completed-tool-actions-across-turns.md`

and add a short index entry under `## Patches` in:

`docs/journal/SPEC-010-agent-loop.md`

The standalone journal must record:

- the original live contradiction;
- why SPEC-010's transient transcript caused it;
- the distinction between reasoning memory and action provenance;
- the exact receipt schema chosen;
- argument/context bounds;
- redaction behavior;
- automated test commands and counts;
- scripted eval result;
- live before/after transcripts;
- model/router provenance;
- reasoning mode;
- model-context overhead;
- TTFT observation;
- implementation commit SHA;
- `--no-ff` merge commit SHA;
- explicit limitation that receipts disappear on application restart.

Do not rewrite the original SPEC-010 history as though action receipts were
always part of the design. Record this as a later correction.

## Out of scope

This PATCH must not include:

- persistent cross-session agent-action memory;
- any `chat_history.json` schema/version change;
- persistence of raw tool-call protocol messages;
- persistence of raw tool results;
- persistence of private reasoning / chain-of-thought;
- summarization or retrieval of old tool observations;
- embeddings, vector memory, RAG, or long-term memory;
- a generic event store;
- new database-backed conversation storage;
- tool-specific memory adapters;
- Tracker-specific or Time-specific logic;
- changing which tools an active skill may see (`PATCH-012-02` owns that);
- changing `SkillRouter` behavior;
- making skill activation persistent across turns;
- replaying old tool calls automatically;
- treating a prior successful call as evidence that its result is still current;
- changing tool-call limits, deadlines, reasoning modes, or model profiles;
- general prompt redesign.

In particular:

```text
prior receipt:
mcp_time__get_current_time(...), result_ok=true
```

means only:

```text
that tool call really happened in that prior turn
```

It does **not** mean:

```text
the old timestamp is still the current time
```

If the user asks for current time again, the agent must still call the current
time tool again.

## Suggested branch and commit conventions

```text
branch:
patch/PATCH-010-05-preserve-completed-tool-actions-across-turns

patch file:
patches/SPEC-010/PATCH-010-05-Preserve-Completed-Tool-Actions-Across-Turns.md

implementation commit:
Preserve completed tool actions across turns (PATCH-010-05)

merge:
Merge PATCH-010-05: preserve completed tool actions across turns
```
