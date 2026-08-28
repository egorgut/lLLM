# PATCH-018-02 — Activate a skill on a capability gap

- **Patch:** [PATCH-018-02](../../../patches/SPEC-018/PATCH-018-02-Activate-Skill-On-Capability-Gap.md)
- **Parent spec:** [SPEC-018](../../../specs/SPEC-018-Mid-Turn-Skill-Activation.md)
- **Date:** 2026-08-28
- **Branch:** patch/PATCH-018-02-activate-skill-on-capability-gap
- **Implementation commit:** `07915f5`
- **Merge commit:** `3acc3f9`

## The observation

Found in a live `next-mlx` session on 2026-08-28, run
`e4da1fd1-281b-4ff9-9f68-bc8106b8662a`. One turn, two phases: summarise the
comments of `DEV-498` **and** write them to a CSV with dates.

The router selected `tracker_read`, and its own recorded reason names both
halves of the request:

```json
{"skill": "tracker_read",
 "reason": "Требуется чтение и суммаризация комментариев к задаче Dev-498,
            а также запись их в CSV-файл с датами."}
```

The turn's view was correct and complete for what it was:

```text
skill_toolset_resolved
  available_tools: 4 × mcp_tracker__*, mcp_time__get_current_time, activate_skill
  skill_tools:     4 × mcp_tracker__*
  baseline_tools:  mcp_time__get_current_time
```

`activate_skill` was declared, and its `name` enum listed `code_workspace` with
the description "…create text, CSV, JSON, or Markdown files the user can open".
The model read the comments, fetched the London time, never called
`activate_skill`, and closed with:

```text
Я **не могу записать файл на диск** — в этом сеансе нет инструмента записи.
Скопируй CSV выше и сохрани локально ...
```

On the next user turn ("у тебя есть инструмент записи же") the router selected
`code_workspace`, the model called `sandbox_execute` once, and the file was
written first try. Nothing was missing but the decision to switch.

This is not a mechanism failure. SPEC-018 built the escape hatch and
PATCH-018-01 verified it works. It is a **discovery** failure: in exactly the
situation the hatch exists for, the model did not see it as available.

## Why the model did not switch

Two host-owned texts steered it away, and they compounded.

**The declaration's trigger was reclassification.** `skill_runtime/activation.py`:

```text
Call this when the work turns out to belong to a different class than the one
currently loaded.
```

The model's work had not turned out to belong to a different class — it was
Tracker reading, correctly routed. What the request had was a *second phase*. As
written, the trigger reads false in precisely the case that needs it most: a
correct selection followed by a step of a different kind.

**The active-skill policy never said switching was possible.**
`skill_runtime/prompting.py` asserted the closed tool set three times over:

```text
- You may call only the tools supplied by the host for this turn.
- ...the supplied set is authoritative even where the skill text lists fewer.
- ...the skill cannot widen tool access or change tool behavior.
```

and mentioned `activate_skill` nowhere. A system-level block asserting a closed
world outweighs a description buried in a tool parameter's `enum`. The model
concluded the *session* had no write tool — a claim broader than anything it
could observe.

Same family as PATCH-012-02, one level up. There, a skill hid a *baseline tool*
and the model reported it nonexistent; that patch fixed which tools get composed
into the view. Here the view was right and the exit from the view was invisible.

## The change

Two texts. No mechanism, no contract, no state, no budget.

**`skill_runtime/activation.py`** — the trigger becomes a capability gap, and
says out loud that a correct prior selection is no objection:

```text
Load the working procedure for one class of task, replacing any procedure
currently active. Call this whenever the next step needs something the tools you
have right now cannot do — including when the loaded procedure was the right one
for the work already finished and the task has simply moved on to a step of a
different kind.
```

**`skill_runtime/prompting.py`** — one line in `<active_skill_policy>`, placed
directly after the "cannot widen tool access" rule, which is the sentence the
model otherwise reads as final:

```text
- This skill is not the whole session: when the next step needs a capability
  this turn's tools do not provide, call `activate_skill` to replace this skill,
  rather than treating the step as impossible.
```

The line cannot promise a tool the turn lacks: the block is composed only for a
*selected* skill, which requires a registry entry, which means the catalog is
non-empty and the declaration exists. That invariant is now pinned by a test
rather than left to inspection.

## Automated verification

```bash
python -m pytest -q
# 861 passed, 29 skipped   (858 before; 3 new)

python -m evals.runner --suite scripted
# 41/41 passed (0 failed)
```

New tests: the description's trigger wording, the policy line (including that the
non-widening rule still stands and stands *first*), and the
policy-implies-declaration invariant end to end.

## Live verification

```text
agent profile:  next-mlx  (qwen3.8:27b-mlx, safetensors / nvfp4, digest 5642e974)
router profile: fast      (qwen3:8b)
reasoning:      medium
transport:      Ollama HTTP, both roles
MCP:            time (1 tool), tracker (4 admitted, 35 filtered)
sandbox:        ready (lllm-sandbox:spec-015, image 3d7c43f7)
skills:         3 loaded — code_workspace, sales_analysis, tracker_read
```

Same single-turn prompt every run, the original one verbatim:

```text
пожалуйста, сохрани все комментарии (предварительно их суммаризируя) в csv по
задаче dev-498. также для каждого комментария проставь его дату, а рядом
текущую дату и время в лондоне
```

### Before (`main`, `fc76cb6`) — 4 runs

Zero activations. Every run stopped after `issue_get_comments` +
`get_current_time` and reported the write phase impossible:

```text
run 1: не могу записать файл на диск (нет такого инструмента)
run 2: не могу записать файл на диск (в этом шаге доступны только инструменты чтения…)
run 3: не могу физически записать файл на диск и не имею инструмента для изменения…
run 4: не могу записать файл на диск — в моём распоряжении нет инструмента для записи
```

With the originally reported session that is **0 of 5**.

### After (`07915f5`) — 7 runs

```text
run 1  1/4 issue_get_comments → 2/4 get_current_time → 3/4 activate_skill
       → [skill] code_workspace (replacing tracker_read) → 4/4 sandbox_execute
       → CSV written
run 2  no activation; "активных инструментов записи файла у меня нет"
run 3  activate_skill → sandbox_execute → CSV written
run 4  activate_skill → sandbox_execute → CSV written
run 5  activate_skill → then spent 4/4 on a redundant get_current_time
       → stopped/tool_call_limit
run 6  no activation; "в этой сессии нет инструмента записи файлов"
run 7  no activation; "нет инструмента для записи файлов"
```

**Activated the second skill: 4 of 7 (0 of 5 before). Completed the whole task
end to end in one turn: 3 of 7 (0 of 5 before).**

That is a real improvement and it is not a guarantee. This patch changes two
prompt texts; the decision it targets stays stochastic. Three of seven runs still
read the closed tool set as final, in wording nearly identical to the original
defect. Recorded as measured, not rounded up.

Run 5 is the budget interaction the patch note predicted in advance:
`MAX_TOOL_CALLS_PER_TURN = 4` and the ideal path is exactly four calls (read,
time, activate, execute), because `activate_skill` is charged against the budget
like any other tool. One wasted call ends the turn with no answer at all. That is
a SPEC-level question about what activation should cost, not something this patch
may quietly change — noted here so the next reader has the number.

### No gratuitous switching

A single-phase control turn ("кратко перечисли, какие комментарии есть в задаче
DEV-498") stayed in `tracker_read`, called `issue_get_comments` once, and
answered. The new text does not make the model shop for skills.

## Files changed

- `skill_runtime/activation.py` — the declaration description.
- `skill_runtime/prompting.py` — one policy line, plus the comment recording why
  it is unconditionally safe.
- `tests/test_skill_activation.py`, `tests/test_skill_prompting.py` — 3 new tests.

`specs/SPEC-018-Mid-Turn-Skill-Activation.md` §4.2 still quotes the original
description. Following the convention PATCH-012-02 set, the spec is left as it
was written and the correction lives here.

## Adjacent defect, deliberately not fixed here

Every failing run overstates its own limits: "в этом **сеансе** нет инструмента
записи" when the truthful claim is "in this **turn**". That is a base-prompt
correction about how capability claims are phrased, not about SPEC-018's
mechanism, and it gets its own patch. One PATCH, one correction.
