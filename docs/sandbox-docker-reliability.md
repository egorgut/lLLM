# Docker as a sandbox dependency — an operational complaint

Written 2026-08-30, from what SPEC-021's live measurement ran into. This is an
observation note, not a spec or a patch: it records a problem with how the
sandbox dependency behaves in practice, so the next person does not rediscover it
by losing an afternoon of runs.

## What happened

During one measurement session the Docker daemon restarted three times without
being asked. Every process that started during an outage came up like this:

```text
[sandbox] unavailable: Build the sandbox image with 'python scripts/build_sandbox_image.py'.
[skills] code_workspace: omitted
[skills] 2 loaded: sales_analysis, tracker_read
```

Ten of fifteen live runs were invalidated. Docker was healthy again minutes
later, and `docker image inspect lllm-sandbox:spec-015` answered in 40 ms — the
image had never actually gone anywhere.

## Why it is worse than a flaky dependency

Three properties compound, and the third is the dangerous one.

1. **Availability is decided once, at startup.** `app.py` calls
   `build_sandbox_capability` exactly once; if it returns `None`, the tool is
   never registered and `omitted_skills` drops `code_workspace` for the life of
   the process. The backend itself is not at fault here — `_resolve_image_id`
   caches a *successful* image ID (deliberately, so a retag between two jobs
   cannot change what the second one executes) and does not cache a failure. But
   nothing ever asks it again, so a process that starts during a ten-second
   outage stays sandbox-less for an hour.

2. **The failure degrades the skill registry, not just the tool.** No sandbox
   means `code_workspace` is omitted from the catalog, so the *router* cannot
   select it and `activate_skill` cannot reach it. A capability disappears from
   the model's world rather than failing when used.

3. **It is silent at the only place it matters.** The `[sandbox]` line is
   printed once at startup and never again. Nothing in the turn, the outcome, or
   `turn_finished` records that this run had a smaller world than the last one.
   A run that could not possibly have written a file is indistinguishable, in the
   trace, from one that chose not to.

Property 3 is what nearly produced a false finding. Runs missing `code_workspace`
look exactly like a model declining to activate a skill — which is precisely the
behaviour PATCH-018-02 studied. Without checking the startup header per run, the
write-up would have been "the model did not activate", with numbers to back it up
and nothing wrong except the premise.

## The message is misleading too

`SandboxImageUnavailable` prints "Build the sandbox image with
'python scripts/build_sandbox_image.py'" — advice that is wrong whenever the
image exists and the daemon is merely unavailable. `docker_backend.py` already
guards this by only raising it when stderr contains `No such image`, and SPEC-015's
own journal flags the same ambiguity. It still fired here on an image that was
present the whole time, so the guard is not sufficient in practice.

## What to do about it

Nothing here is urgent enough to be its own SPEC on its own, but two of these are
small:

- **Make the degraded world visible in the trace.** `run_started` records the
  model and profile; it should record whether the sandbox resolved and which
  skills actually loaded. Then a trace answers "could this run have written a
  file?" without anyone having kept the terminal output. *This is the one worth
  doing first — it is additive, and it is what turns a silent failure into a
  measurable one.*
- **Re-probe instead of deciding once.** Resolving the image to an immutable ID
  on first success is correct and should stay. Deciding *unavailability* once,
  at startup, for the whole process is what hurts: a ten-second outage should not
  permanently remove a capability from a long-lived session. Note this is not a
  free change — SPEC-016 §11.2 omits the skill precisely so the model is never
  offered a capability that could only fail, and a capability that appears
  mid-session would have to be composed into the registry and the catalog safely.
- **Say what actually happened.** Distinguish "daemon unreachable" from "image
  missing" in the user-facing line, rather than defaulting to build advice.

## For anyone running a live corpus in the meantime

Assert preconditions per run, not once at the start, and mark a run invalid
rather than measuring it:

```zsh
docker image inspect lllm-sandbox:spec-015 --format '{{.Id}}' >/dev/null 2>&1 || skip
...
grep -q "code_workspace, sales_analysis, tracker_read" "$out" || echo "INVALID"
```

A live corpus that does not check its own preconditions will happily produce
clean, wrong numbers.
