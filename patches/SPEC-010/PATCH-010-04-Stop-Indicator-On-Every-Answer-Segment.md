# PATCH-010-04 — Stop the indicator on every answer segment

## Parent spec

`specs/SPEC-010-Agent-Loop.md`

Corrects the renderer lifecycle introduced by
`patches/SPEC-010/PATCH-010-01-CLI-Turn-Activity-Indicator.md`.

## Problem

`CliRenderer.text()` stops the activity indicator only on the turn's **first**
answer segment, because the stop is nested inside the `_printed_prefix` guard
that exists for a different purpose — printing `Qwen: ` exactly once:

```python
def text(self, chunk: str) -> None:
    if not self._printed_prefix:
        self._indicator.stop()          # only the first time
        print("\nQwen: ", end="", flush=True)
        self._printed_prefix = True
    print(chunk, end="", flush=True)
```

One flag is serving two unrelated decisions. "Has the prefix been printed?" is
genuinely once per turn. "Is the CLI still silent?" is not — the indicator is
restarted by `tool_call()` and `tool_result()` after every tool.

So when a turn has the shape **text → tool call → text**, the second stretch of
answer text is streamed while the indicator is still animating. Both write to the
same line using `\r`, and the answer is destroyed:

```text
⠙ Working... 56.8sтимакросом:язанные с Оп
⠋ Working... 57.9s | Статус |нитель
⠦ Working... 1m 02.3s |ешён✅ав Павли | И
```

The shape is not exotic. It happens whenever the model comments before reaching
for another tool — observed live on a Tracker search where the first query
returned nothing and the model said so ("По названию ничего не нашёл. Попробую
поискать в описании задач.") before issuing a second `issues_find`.

It normally stays hidden because a tool-selecting response usually streams no
text at all: `ModelResponse.text_chunks` stops yielding at the first tool call.
The defect needs a response that emits text *and then* a tool call.

This is a PATCH-010-01 defect, not a PATCH-010-03 one. PATCH-010-03 only made it
destructive rather than merely ugly: a static `⠋ Working...` overwrote the same
13 characters every frame, while a counter changes both its content and its width
on every frame, so it shreds a different part of the answer each time.

## Expected change

Separate the two decisions. Arriving text means the silence is over, whatever
segment it belongs to, so the indicator is stopped unconditionally; the prefix
guard keeps doing only its own job.

```python
def text(self, chunk: str) -> None:
    self._indicator.stop()
    if not self._printed_prefix:
        print("\nQwen: ", end="", flush=True)
        self._printed_prefix = True
    print(chunk, end="", flush=True)
```

`stop()` is idempotent and returns after one lock acquisition when nothing is
running (`cli_activity.py`), so calling it per chunk is free in practice and
keeps the rule simple: no state decides whether to stop, text alone does.

## Constraints

- One correction only. The Tracker MCP child process writing its own `INFO`
  logging to the same terminal is a separate, pre-existing source of interleaved
  output and must not be addressed here.
- Preserve the `Qwen: ` prefix contract exactly: printed once per turn, only when
  real answer text arrives, never for a turn that resolves purely through tools.
- Preserve every PATCH-010-01 guarantee: non-TTY silence, `\r` and spaces only,
  the animation thread joined before `stop()` returns.
- No change to agent policy, prompts, tool contracts, transport, tracing, or
  anything the model can observe.
- Framework-free, standard library only.

## Acceptance criteria

- In a **text → tool → text** turn the indicator is stopped before the second
  segment of answer text is written, so no frame can interleave with it.
- The stop happens for every answer segment, not only the first.
- `Qwen: ` still appears exactly once per turn, and not at all for a turn that
  produces no answer text.
- The existing renderer-ordering and two-tool-turn assertions still hold.
- Non-TTY output stays byte-for-byte unchanged.
- The full deterministic suite and the scripted eval suite pass.
- No live-model verification required: nothing the model sees changes. A live
  reproduction is nevertheless worth capturing, since the defect was found live.

## Files likely affected

- `app.py`
- `tests/test_cli_activity.py`
- `docs/journal/SPEC-010-agent-loop.md`

This list is advisory, not restrictive.

## Verification

Automated:

```bash
python -m pytest tests/test_cli_activity.py -q
python -m pytest -q
python -m evals.runner --suite scripted
```

The regression test drives a real `AgentRunner` through the failing shape — a
scripted response carrying **both** text and a tool call, then a second response
with the final answer — and asserts the exact marker/output interleaving, the way
`test_activity_resumes_between_every_step_of_a_two_tool_turn` already does. Before
the fix the final answer line appears with no preceding `<stop>`; after it, the
stop is there. A second test pins the prefix contract, so the fix cannot be
mistaken for "print `Qwen: ` again".

End to end: `app.py` in a pty against a live model, asking something that makes it
comment and then call another tool. Replaying the carriage returns must leave no
line where answer text and a spinner frame share a row.

## Journal strategy

Append a `### PATCH-010-04` subsection to the parent journal
`docs/journal/SPEC-010-agent-loop.md`, under its existing `## Patches` heading —
deterministic presentation only, no model-facing or model-decision impact
(`patches/README.md` → Journal rules), as PATCH-010-01, -02, and -03 all did.

## Out of scope

- The Tracker MCP child's `INFO` logging landing on the same terminal.
- Printing `Qwen: ` again for a continued answer, or any other change to how
  answer segments are labelled or separated.
- Any change to the elapsed counter or the `[time]` footer from PATCH-010-03.
- Reworking `ModelResponse.text_chunks` so a tool-calling response drops its
  text — that changes what the user is shown about the model's reasoning and
  would need its own patch, with evidence.
- Buffering, locking, or otherwise coordinating writers to stdout in general.

## Suggested branch and commit conventions

Per the repository PATCH workflow:

```text
branch:
patch/PATCH-010-04-stop-indicator-on-every-answer-segment

patch file:
patches/SPEC-010/PATCH-010-04-Stop-Indicator-On-Every-Answer-Segment.md

implementation commit:
Stop the activity indicator on every answer segment (PATCH-010-04)

merge:
Merge PATCH-010-04: stop indicator on every answer segment
```
