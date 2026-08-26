# SPEC-020 — Reasoning-Aware Agent Turn

**Status:** Proposed  
**Step:** 20  
**Depends on:** SPEC-010, SPEC-011, SPEC-017, SPEC-019  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

The current agent loop already pays for model reasoning, but it treats that reasoning
as disposable transport noise.

For a thinking-capable model such as Qwen3.8, one agent step currently looks like:

```text
user / working transcript
        │
        ▼
model reasoning
        │
        ├── hidden from the user
        ├── ignored by the harness
        └── discarded
        │
        ▼
tool call
        │
        ▼
tool result
        │
        ▼
next model request
```

`llm.ModelResponse.text_chunks()` reads streamed Ollama messages, but only yields
`message.content`; `message.thinking` is ignored. When the model requests a tool,
`AgentRunner` appends only the assistant tool call and the tool result to its transient
working transcript.

The next model decision therefore receives the action and observation, but not the
reasoning that led to that action.

This step makes reasoning a first-class **transient internal state of one active agent
turn**:

```text
reasoning
   ↓
tool call
   ↓
tool result
   ↓
previous reasoning + action + observation
   ↓
continued reasoning
   ↓
next action / final answer
```

The reasoning remains private:

```text
✓ may be returned to the model inside the current agent turn
✗ is never rendered to the user
✗ is never persisted to chat_history.json
✗ is never written as text or hash to traces
✗ is never copied into a tool result
✗ never survives into the next user turn
```

The step also makes reasoning latency observable without exposing its content. The
runtime records timing/count metrics that distinguish:

```text
request started
    ↓
first model output (thinking or content)
    ↓
first thinking token
    ↓
first user-visible content / tool call
    ↓
response finished
```

Finally, the agent-side reasoning mode becomes an explicit host-owned run setting so
Qwen3.8 can be compared under:

```text
auto    -> preserve the model/server default
off     -> think=False
low     -> think="low"
medium  -> think="medium"
```

`auto` is the backward-compatible default. Skill routing remains `think=False`
unconditionally, exactly as PATCH-012-01 requires.

This SPEC does **not** claim that reasoning preservation or lower reasoning effort will
automatically improve first-token latency. It adds the mechanism and the measurements
needed to determine where the time is actually spent.

---

## 2. Motivation

### 2.1 The harness currently pays for reasoning and then throws it away

The agent transport calls Ollama with `think=None` unless a caller explicitly overrides
it. For thinking-capable models, Ollama may therefore emit `message.thinking` before
`message.content`.

The current `ModelResponse` deliberately ignores that field.

For a tool-assisted step this means the model may spend substantial time deriving a
plan such as:

```text
I need sales for both periods first.
Then calculate the delta.
If the decline is concentrated, inspect the customer mix.
```

and then emit:

```text
tool_call: sql_query(...)
```

After the tool executes, the next request receives only:

```text
assistant: tool_call sql_query(...)
tool: {...result...}
```

The prior plan is gone.

The model must infer its previous intent again from the visible action and observation.
For long agent chains this is unnecessary repeated work and weakens continuity between
decisions.

### 2.2 Qwen3.8 is designed to carry reasoning through agent work

The Qwen3.8 Ollama package exposes thinking separately from final content and describes
reasoning-context preservation as useful for long-horizon and agentic work.

The installed project dependency, `ollama==0.6.2`, already models assistant messages
with an optional `thinking` field and accepts such messages in chat history.

The project therefore does not need a second hidden-memory subsystem. It needs to stop
discarding an existing part of the model message and preserve it only in the
already-existing per-turn working transcript.

### 2.3 Perceived TTFT is currently ambiguous

The CLI renders only `message.content`.

A request may therefore behave like:

```text
0.0 s  request starts
0.8 s  first thinking chunk arrives
0.8–18 s model reasons
18.4 s first content chunk arrives
```

The user experiences roughly 18 seconds of silence and calls that "TTFT", while the
model actually started generating after 0.8 seconds.

The current trace records only the total duration of the model request. It cannot
separate:

- model load;
- prompt evaluation;
- hidden reasoning;
- time to first user-visible content;
- final answer generation.

That makes comparisons with `ollama run` misleading because the Ollama CLI may visibly
stream thinking while the lLLM CLI intentionally hides it.

### 2.4 Reasoning effort must be an experiment, not an assumption

The agent currently uses the model/server default reasoning behavior.

For Qwen3.8 that default may be intentionally deep. A simple conversational question
and a multi-tool analysis therefore enter the same agent transport with the same
implicit policy.

This SPEC adds explicit host control, but no automatic task classifier:

```text
auto / off / low / medium
```

The user chooses the mode before the run starts. The live evaluation records latency,
tool behavior, and answer quality for each mode.

No claim that `low` is "best" belongs in production code.

---

## 3. Goals

### 3.1 Functional

- Capture streamed model reasoning separately from user-visible content.
- Preserve reasoning across successive model decisions inside one active agent turn.
- Return preserved reasoning to the model together with the assistant tool call and
  subsequent tool result.
- Keep reasoning completely hidden from the terminal.
- Keep reasoning out of persistent conversation history.
- Keep reasoning text and hashes out of traces.
- Add non-content reasoning metrics to model-response tracing.
- Measure first model output separately from first user-visible output.
- Add an explicit host-owned reasoning mode for the **agent** model:
  `auto`, `off`, `low`, `medium`.
- Keep `auto` as the backward-compatible default.
- Keep skill routing permanently on its existing `think=False` path.
- Produce live evidence showing whether preservation and effort control improve or
  worsen latency and agent behavior on the target Qwen3.8 profile.

### 3.2 Architectural

- Reasoning is **ephemeral turn state**, not conversation memory.
- Reuse the existing `working_messages` transcript in `AgentRunner`; do not introduce
  a second reasoning store.
- Keep `conversation.py` semantic: stored history remains only user messages and final
  assistant answers.
- Keep tool results unchanged; reasoning is assistant state, not observation data.
- Keep the renderer reasoning-blind.
- Keep model-specific policy out of `AgentRunner`.
- Keep reasoning selection host-owned and fixed for one process run.
- Keep routing behavior independent from agent reasoning configuration.
- Add no third-party dependency.

### 3.3 Non-goals

Listed in §9.

---

## 4. Functional requirements

### 4.1 Reasoning mode

Introduce one small host-owned reasoning setting for the agent transport.

Conceptually:

```python
ReasoningMode = Literal["auto", "off", "low", "medium"]
```

CLI:

```bash
python app.py --reasoning auto
python app.py --reasoning off
python app.py --reasoning low
python app.py --reasoning medium
```

Rules:

- omitted `--reasoning` => `auto`;
- `auto` => do not override Ollama's model default (`think=None`);
- `off` => `think=False`;
- `low` => `think="low"`;
- `medium` => `think="medium"`;
- invalid values fail before the chat loop starts;
- the setting applies only to the agent-side `respond()` transport;
- `SkillRouter` continues to call `OllamaModel.text()` with `think=False`
  regardless of this setting.

Do not add `high` or `xhigh` as an explicit option in this SPEC.

Reason: the installed Ollama Python SDK accepts `high`, while the current Qwen3.8
template's highest named effort is `xhigh`. `auto` already preserves that model/server
default without inventing an unsafe mapping between the two names.

If a later Ollama version exposes a stable native `xhigh` contract for this package,
adding it is a separate PATCH.

### 4.2 Transport ownership of the mode

Reasoning configuration belongs at the model-transport composition boundary, not in
the loop.

Conceptually, the agent `OllamaModel` is constructed with its default reasoning mode:

```python
agent_model = OllamaModel.for_profile(
    roles.agent,
    reasoning_mode=reasoning_mode,
)
```

`respond()` uses that mode unless an internal specialized call explicitly overrides it.

`text()` remains authoritative for routing and still forces:

```python
think=False
format=ROUTING_RESPONSE_SCHEMA
```

When SPEC-019 resolves the same profile for router and agent, one transport object may
still be reused only if its routing `text()` path remains an explicit override and
cannot inherit the agent reasoning mode.

The exact constructor shape is implementation-defined; the ownership rule is not.

### 4.3 Stream contract

The current transport contract exposes only user-visible text chunks and tool calls.
It must be extended so the caller can distinguish reasoning from final content while
still streaming.

A small structured chunk is preferred, for example:

```python
@dataclass(frozen=True)
class ModelStreamChunk:
    thinking: str = ""
    content: str = ""
```

and:

```python
class ModelResponseLike(Protocol):
    def chunks(self) -> Iterator[ModelStreamChunk]: ...
    @property
    def tool_calls(self) -> list[ModelToolCall]: ...
```

Equivalent naming is allowed.

Requirements:

- every non-empty `message.thinking` fragment is surfaced as `thinking`;
- every non-empty `message.content` fragment is surfaced as `content`;
- tool calls remain separately collected as today;
- the transport must not concatenate thinking into content;
- the transport must not print anything;
- a non-thinking model produces empty reasoning and otherwise behaves normally.

This is a transport-level distinction, not a Qwen-specific parser.

### 4.4 Agent consumption

For each model request, `AgentRunner` accumulates independently:

```text
thinking_parts
content_parts
tool_calls
```

Behavior:

- thinking chunks are accumulated but never sent to `Renderer.text`;
- content chunks continue to stream immediately to `Renderer.text`;
- existing timeout / abandonment behavior applies to both;
- tool calls remain authoritative exactly as today;
- a final textual answer still returns only the joined content;
- accumulated reasoning is discarded when the turn terminates.

The result of consuming one model response should be one small internal decision object
or equivalent carrying at least:

```text
text
thinking
tool_calls
timing metrics
```

Do not expose reasoning through `AgentTurnOutcome.final_text`.

### 4.5 Transient reasoning preservation

When a model decision emits a tool call, the assistant message appended to
`working_messages` must include the reasoning produced by that same decision.

Current shape:

```python
{
    "role": "assistant",
    "content": "",
    "tool_calls": [...]
}
```

Target shape:

```python
{
    "role": "assistant",
    "content": "",
    "thinking": captured_reasoning,
    "tool_calls": [...]
}
```

followed by the existing tool result message:

```python
{
    "role": "tool",
    "tool_name": "...",
    "content": "...serialized result..."
}
```

The next model request therefore sees:

```text
prior semantic conversation
+ current-turn reasoning
+ assistant tool call
+ tool result
```

Rules:

- only reasoning from the current model decision is attached to that assistant message;
- the reasoning remains in `working_messages` only;
- it is never copied to `Conversation`;
- it is never copied into the tool result;
- an empty reasoning string may be omitted entirely;
- repeated model/tool steps preserve their own reasoning independently.

### 4.6 Reasoning lifetime

The lifetime boundary is the active call to `AgentRunner.run_turn`.

```text
turn starts
    ↓
reasoning may accumulate in working_messages
    ↓
zero or more tool calls
    ↓
final answer / failure / timeout / cancel
    ↓
working_messages discarded
```

A successful turn persists only:

```text
user message
final assistant content
```

A failed/cancelled turn continues to follow the existing rollback rules.

No reasoning from turn N may be included in the model-facing conversation of turn N+1.

### 4.7 Mid-turn skill activation

`activate_skill` is an ordinary model tool decision for the purpose of reasoning
preservation.

If reasoning leads to:

```text
activate_skill(...)
```

the associated reasoning stays attached to that assistant tool-call message before the
activation result is appended.

The subsequent system-suffix replacement from SPEC-018 remains authoritative.

Reasoning preservation must not:

- stack skill system blocks;
- bypass a skill allowlist;
- change activation limits;
- turn prior reasoning into trusted host instruction.

Prior reasoning is historical assistant state only.

### 4.8 User-visible behavior

Reasoning remains invisible.

The CLI must continue to show only:

```text
activity indicator
[skill] ...
[tool ...] ...
[result] ...
Qwen: final content
```

No new `Thinking:` section is added.

A reasoning-heavy model request may therefore still be visually silent until a tool
call or content arrives. The difference is that the trace can now explain that silence
without exposing the reasoning text.

### 4.9 Latency metrics

The runtime must distinguish model activity from visible answer latency.

For each agent model request, add additive fields to the existing
`model_response_finished` event, at minimum:

```text
thinking_chars
first_model_output_ms
first_thinking_ms
first_content_ms
first_tool_call_ms
visible_ttft_ms
```

Semantics:

- `thinking_chars`: total captured reasoning characters;
- `first_model_output_ms`: request start -> first non-empty thinking/content/tool event;
- `first_thinking_ms`: request start -> first reasoning fragment, or `null`;
- `first_content_ms`: request start -> first user-visible content fragment, or `null`;
- `first_tool_call_ms`: request start -> first tool-call fragment, or `null`;
- `visible_ttft_ms`: request start -> first thing the harness can show as model output:
  first content or first tool call, whichever occurs first.

Exact event field names may differ if documented, but these distinctions must survive.

The event keeps its existing total `duration_ms`.

### 4.10 Ollama timing metadata

When available in the final streamed Ollama response, capture additive transport metrics
without changing model behavior:

```text
ollama_load_ms
ollama_prompt_eval_ms
ollama_prompt_eval_count
ollama_eval_ms
ollama_eval_count
```

If a field is absent, record `null` or omit it consistently.

These values are diagnostic only. They do not alter deadlines or turn policy.

The purpose is to distinguish:

```text
model loading
vs prompt evaluation
vs token generation
vs hidden reasoning
```

when investigating latency.

### 4.11 Trace privacy

Reasoning content is forbidden in tracing.

Do not write:

```text
thinking
thinking_preview
thinking_sha256
reasoning_excerpt
reasoning_tokens_as_text
```

Only non-content metrics are allowed.

This rule applies to:

- normal traces;
- errors;
- timeout diagnostics;
- live eval result files;
- journal transcripts.

The journal may report counts and timings, but never copy the hidden reasoning text.

### 4.12 Startup visibility

When the agent reasoning mode differs from the default or when displaying the resolved
run configuration, make the mode visible.

Example:

```text
[model] agent next: qwen3.8:27b (...)
[router] fast: qwen3:8b (...)
[reasoning] auto (transient preservation on)
```

or:

```text
[reasoning] off
```

Exact wording may differ.

The user must be able to tell which reasoning mode produced a live measurement without
opening source code.

### 4.13 Evaluation result identity

Live-eval output must record the selected reasoning mode, for example:

```text
reasoning_mode
```

Existing model/profile/router identity from SPEC-019 remains unchanged.

No reasoning text enters eval results.

---

## 5. Design constraints

- **Reasoning is transient.** It may live only in the active agent turn's working
  transcript.
- **Conversation remains semantic.** `conversation.py` stores no reasoning and receives
  no new hidden-message type.
- **Renderer remains blind.** No reasoning text is printed, even in debug mode.
- **Tracing remains content-free.** Counts and timings only.
- **Router is unchanged.** Skill routing remains `think=False` and schema-constrained.
- **No model-specific branching in the loop.** `AgentRunner` handles an optional
  reasoning field generically.
- **No second memory mechanism.** Use the existing assistant message representation and
  `working_messages`.
- **No automatic effort routing.** The host chooses one mode for the run.
- **Default is backward compatible.** `auto` reproduces the current agent-side reasoning
  request behavior.
- **Tool policy is unchanged.** Reasoning cannot bypass call budgets, allowlists,
  deadlines, control-tool handling, or repeated-call detection.
- **Framework-free.** No new third-party dependency.

---

## 6. Acceptance criteria

- [ ] `app.py` accepts `--reasoning auto|off|low|medium`; omitted means `auto`.
- [ ] Live eval accepts the same agent reasoning setting.
- [ ] `auto` leaves agent `think` unset / model-default.
- [ ] `off` sends `think=False` for agent requests.
- [ ] `low` and `medium` are passed through as native Ollama thinking values.
- [ ] Skill routing still always sends `think=False`, independent of agent reasoning
      mode.
- [ ] Stream handling captures `message.thinking` separately from `message.content`.
- [ ] Reasoning chunks are never passed to `Renderer.text`.
- [ ] Content chunks still stream to the renderer immediately.
- [ ] A tool-emitting model decision appends its captured reasoning to the transient
      assistant tool-call message.
- [ ] The next model request receives that assistant reasoning + tool call + tool result.
- [ ] Multi-tool turns preserve reasoning independently for every model decision.
- [ ] Final `AgentTurnOutcome.final_text` contains only user-visible content.
- [ ] `Conversation.stored_messages` never contains a `thinking` field.
- [ ] `chat_history.json` never persists reasoning.
- [ ] A timeout, failure, cancellation, `/reset`, or normal turn completion leaves no
      reasoning state available to the next turn.
- [ ] `model_response_finished` records reasoning/TTFT metrics without reasoning text.
- [ ] Where Ollama supplies final timing metadata, load/prompt-eval/eval metrics are
      captured additively.
- [ ] No trace or eval result contains reasoning text, a reasoning preview, or a hash of
      reasoning text.
- [ ] Existing tool budgets, skill restrictions, control-tool behavior, deadlines, and
      rollback semantics are unchanged.
- [ ] Existing deterministic tests pass unchanged apart from additive assertions.
- [ ] No new third-party dependency.

---

## 7. Verification

### 7.1 Automated

Add deterministic tests covering at least:

1. a stream containing only content;
2. a stream containing thinking then content;
3. a stream containing thinking then one tool call;
4. a two-tool agent turn where the second model request receives the first decision's
   reasoning;
5. a non-thinking response where no `thinking` field is added to the transcript;
6. reasoning never reaching `CliRenderer.text`;
7. final conversation persistence containing only user/final-assistant semantic
   messages;
8. timeout/cancel/error paths discarding transient reasoning;
9. trace fields containing counts/timings but no reasoning text or digest;
10. mapping of `auto`, `off`, `low`, `medium`;
11. router calls remaining `think=False` under every agent reasoning mode;
12. same-profile and split-profile SPEC-019 paths both honoring the agent mode correctly.

Run:

```bash
pytest
python -m evals.runner --suite scripted
```

No automated test contacts a live model.

### 7.2 Live — preservation proof

Use Qwen3.8 as the agent and the fast router:

```bash
python app.py --profile next --router-profile fast --reasoning auto
```

Run at least one committed multi-step case that requires:

```text
model decision
→ tool
→ model decision
→ tool or final answer
```

The trace must prove, without showing reasoning text:

```text
request 1: thinking_chars > 0
request 2: receives the transient assistant message
turn completes normally
```

Add one dedicated diagnostic test double or safe structural assertion if needed to
prove the second request includes a non-empty `thinking` field; the live trace itself
must not reveal the value.

### 7.3 Live — reasoning-mode comparison

Run the **same prompts, same commit, same model package, same router, same tool
availability** under:

```bash
python -m evals.runner --suite live --profile next --router-profile fast --reasoning auto
python -m evals.runner --suite live --profile next --router-profile fast --reasoning off
python -m evals.runner --suite live --profile next --router-profile fast --reasoning low
python -m evals.runner --suite live --profile next --router-profile fast --reasoning medium
```

At minimum compare:

| metric | auto | off | low | medium |
| --- | ---: | ---: | ---: | ---: |
| routing duration | | | | |
| first model output | | | | |
| visible TTFT | | | | |
| thinking chars | | | | |
| prompt eval duration | | | | |
| eval duration | | | | |
| total turn duration | | | | |
| model requests | | | | |
| tool sequence correct | | | | |
| task result correct | | | | |

The journal must distinguish **engine activity latency** (`first_model_output_ms`) from
**perceived latency** (`visible_ttft_ms`).

Do not conclude that a reasoning mode is faster from total duration alone if it changes
the number of model/tool steps or output length.

### 7.4 Live — preservation A/B

The implementation must include a deterministic test switch or evaluation-only path
that allows preservation to be disabled **without changing normal production default**
so the milestone can compare:

```text
reasoning generated + discarded between tool steps
vs
reasoning generated + preserved between tool steps
```

This switch must not become a general user-facing runtime mode unless needed for the
evaluation harness.

Compare on the same multi-tool cases:

```text
total turn duration
per-request duration
thinking chars after the first tool
tool-call count
repeated/redundant calls
task correctness
```

The purpose is to establish whether preservation actually reduces repeated reasoning on
the target package rather than merely assuming it.

### 7.5 Journal

Create:

```text
docs/journal/SPEC-020-reasoning-aware-agent-turn.md
```

Record:

- repository commit;
- Ollama version;
- `ollama==0.6.2` client version;
- exact agent model name, digest, quantization, parameter count, context length;
- exact router model identity;
- reasoning mode;
- whether transient preservation was enabled;
- all §7.3 comparison metrics;
- preservation A/B result;
- representative user-visible transcript with **no hidden reasoning text**;
- final conclusion:
  - does reasoning preservation help multi-step turns?
  - which reasoning mode gives the best latency/quality trade-off?
  - how much of perceived TTFT was hidden reasoning vs load/prompt evaluation?

The conclusion belongs in the journal. The runtime must not encode the winning mode
automatically.

---

## 8. Risks

- **Preserved reasoning increases prompt size.** Returning prior reasoning to the model
  may make prompt evaluation slower even if it reduces repeated reasoning. Mitigation:
  §7.4 measures both sides.
- **Reasoning can be very large.** A long trace can consume context and KV-cache space.
  This SPEC intentionally does not invent a truncation/summarization policy; measure the
  behavior first. If bounding is needed, it becomes a focused follow-up.
- **Effort levels may not behave identically across model packages.** `low` and `medium`
  are transport settings, not universal semantic guarantees. The journal records the
  exact package and observed behavior.
- **Qwen3.8 highest-tier naming differs from generic Ollama naming.** This SPEC avoids an
  explicit `high`/`xhigh` mapping and uses `auto` for the package default.
- **Latency metrics can be misread.** A long visible TTFT may be hidden reasoning rather
  than model loading. The separate first-output/content/load/prompt-eval metrics are
  required precisely to avoid that conclusion.
- **Reasoning text is sensitive internal state.** A logging shortcut could accidentally
  persist it. Tests must assert absence from traces, eval files, and chat history.
- **Model behavior can regress while latency improves.** `off` may be faster but make
  worse tool decisions. The live comparison must score correctness alongside latency.

---

## 9. Out of scope

- Showing hidden reasoning to the user.
- A `/thinking` command or runtime reasoning-mode changes after the process starts.
- Persisting reasoning across user turns or sessions.
- Storing reasoning in `chat_history.json`, SQLite, files, embeddings, or any memory
  subsystem.
- Logging reasoning text, previews, or hashes.
- Summarizing/compressing/truncating reasoning.
- A token budget for reasoning.
- Automatic task-based selection of `off` / `low` / `medium` / default reasoning.
- An explicit `high` -> `xhigh` mapping for Qwen3.8.
- Changing the skill router's `think=False` contract.
- Removing the initial skill router.
- Model preloading, Ollama `keep_alive`, model-switch optimization, or router/agent
  residency management.
- Switching from native Ollama `/api/chat` to OpenAI-compatible endpoints.
- Switching the runtime to MLX directly.
- Changing temperature, top-p, `num_predict`, context size, or other sampling options.
- Changing model profiles or their deadlines solely because one reasoning mode wins a
  benchmark.
- Claiming that this SPEC alone solves first-token latency. It makes the relevant
  latency components measurable and gives the host explicit reasoning control.
