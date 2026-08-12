# PATCH-017-01 — Add a `mid` model profile for `qwen3:14b`

## Parent spec

`specs/SPEC-017-Model-Profiles.md`

## Problem

SPEC-017 made the model a selectable host-owned profile and shipped two of
them: `fast` (`qwen3:8b`) and `deep` (`qwen3:32b`). The measured gap between
them is large — a routing response takes ~3.6 s against ~11.3 s, and one
tool-emitting agent decision ~11 s against ~40–47 s — so in practice a turn is
either fast and weaker, or strong and slow enough that a four-call turn needs a
600-second budget.

`qwen3:14b` is now pulled on the target machine and sits in that gap, but it
cannot be selected: `MODEL_PROFILES` has no entry for it, and `--profile`
derives its choices from that dict. Selecting it today would mean editing
`config.py` per run — exactly the situation SPEC-017 removed.

## Expected change

One additional entry in `config.MODEL_PROFILES`:

```python
"mid": ModelProfile("mid", "qwen3:14b", 180, 300, 40),
```

Nothing else in the code changes. `--profile` in both `app.py` and
`evals/runner.py` uses `choices=sorted(MODEL_PROFILES)`,
`validate_model_profiles()` iterates the whole dict, and
`resolve_model_profile`'s error message lists every valid name — a third entry
is picked up by all of them without edits.

### Where the deadlines come from

They are **interpolated, not measured**. The owner of the project chose to skip
the live measurement pass and verify the profile in use instead (see
Verification). The numbers are derived from SPEC-017 §2.1's measured table by
parameter count (8.2B → 14.8B → 32.8B, i.e. 14.8B sits 0.27 of the way up),
keeping the same headroom ratio the two committed profiles already use:

| decision | `qwen3:8b` (measured) | `qwen3:14b` (estimated) | `qwen3:32b` (measured) |
| --- | --- | --- | --- |
| skill-routing response | ~3.6 s | ~5.7 s | ~11.3 s |
| agent decision emitting a tool call | ~11 s | ~20 s | ~40–47 s |

Applied to SPEC-017 §2.1's turn shape (`routing + 4 × decision + final
answer` ≈ 104 s for a four-call turn) with the same ~2.5–2.9× margin the
existing profiles carry, this gives request 180 s, turn 300 s, routing 40 s —
each strictly between `fast` (120/180/30) and `deep` (300/600/60).

These are estimates and are labelled as such in the code, the README, and the
journal. If a live run produces a `turn_timed_out` outcome that is ordinary
`mid` latency rather than a model failure, the correction is a follow-up
PATCH-017-02 with the observed numbers — not a silent edit of these values.

## Constraints

- Preserve the parent SPEC's architecture and intent: profiles stay host-owned
  (SPEC-011 §10), the model never sees or influences a profile.
- `fast` stays the default and keeps its exact values, so every journal from
  SPEC-005 onwards remains reproducible.
- No new `ModelProfile` field. Sampling parameters, `num_ctx`, `keep_alive`,
  and `think=False` remain out of scope per SPEC-017 §9; adding one would make
  this a SPEC, not a PATCH.
- No prompt, tool-declaration, agent-loop, or trace-schema change.
- Framework-free, standard library only, no new dependency.

## Acceptance criteria

- `MODEL_PROFILES["mid"]` exists and binds `qwen3:14b`.
- Its three deadlines are strictly between `fast`'s and `deep`'s, and it passes
  `reliability.validate_reliability_config` through the existing
  `validate_model_profiles()` startup check.
- `python app.py --profile mid` starts and reports `[model] mid: qwen3:14b …`;
  `python -m evals.runner --suite live --profile mid` accepts the name.
- `python app.py` with no flag is unchanged: `fast` / `qwen3:8b` / 120/180/30.
- An unknown `--profile` value is rejected at startup and now lists
  `deep, fast, mid`.
- The existing deterministic suite passes unchanged; the scripted eval suite is
  unaffected.

## Files likely affected

- `config.py`
- `README.md`
- `docs/journal/patches/PATCH-017-01-mid-model-profile.md` (new)
- `docs/journal/SPEC-017-model-profiles.md` (index entry)

This list is advisory, not restrictive.

## Verification

**Deliberate deviation from the PATCH flow, on the project owner's explicit
instruction:** this patch adds a model, which normally requires live-model
verification (`patches/README.md` → Verification rules). The owner chose to
skip the measurement pass and the scripted live session and to exercise the
profile personally instead. The deviation is recorded here and in the journal
rather than being papered over, and it is the reason the deadlines above are
estimates.

What is actually run:

```bash
python -m pytest -q                       # no regression from the third entry
python -m evals.runner --suite scripted   # profile-independent, must not move
python app.py --profile huge              # argparse rejects, listing deep, fast, mid
python -c "import config; print(config.resolve_model_profile('mid'))"
```

Two existing tests cover the new profile without modification:
`tests/test_model_profiles.py::TestCommittedProfiles::test_every_committed_profile_is_internally_coherent`
validates every committed profile, and
`TestResolveModelProfile::test_unknown_profile_names_the_valid_ones` iterates
`MODEL_PROFILES` and requires `mid` to appear in the failure message. No new
test is added, also on the owner's instruction.

Left to the owner: `python app.py --profile mid` end to end (plain question,
single-tool question, multi-tool question) and
`python -m evals.runner --suite live --profile mid`.

## Journal strategy

Standalone: `docs/journal/patches/PATCH-017-01-mid-model-profile.md`, indexed
from a new `## Patches` section in `docs/journal/SPEC-017-model-profiles.md`.
A new model changes observable model behaviour, which is the standalone case in
`patches/README.md` → Journal rules, even though the code change is one line.

## Out of scope

- Making `mid` the default profile — `fast` stays the default.
- Any new `ModelProfile` field (sampling, `num_ctx`, `keep_alive`, per-role
  models). SPEC-017 §9 deferred these; they belong to a future SPEC.
- Runtime profile switching inside a session.
- The two stale documentation references noticed while working here
  (`evals/README.md` still mentions the removed `config.MODEL_NAME`;
  `README.md`'s skills configuration block still shows
  `SKILL_ROUTING_TIMEOUT_SECONDS` as a flat constant). One PATCH covers one
  correction — these get their own.
