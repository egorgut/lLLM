# PATCH-019-01 — `qwen3.8:27b-mlx` as an agent runtime (evaluation)

- **Patch:** [PATCH-019-01](../../../patches/SPEC-019/PATCH-019-01-Evaluate-Qwen3.8-27B-MLX-Runtime.md)
- **Parent spec:** [SPEC-019](../../../specs/SPEC-019-Component-Specific-Model-Profiles.md)
- **Date:** 2026-08-25
- **Branch:** patch/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime
- **Implementation commit:** `<pending>`
- **Merge commit:** `<pending>`

## Hypothesis / intent

SPEC-019 made it possible to hold the router constant and change only the agent
profile. That is exactly the shape a runtime comparison needs, so this patch used
it: add one experimental profile bound to Ollama's MLX-backed package for the same
model family as `next`, and measure — without touching the router, prompts, tools,
the agent loop, deadlines, or sampling.

The question is narrow: **is `qwen3.8:27b-mlx` a better runtime for `lLLM` on this
host than `qwen3.8:27b`?** The patch evaluates the candidate. It does not adopt it,
and `DEFAULT_MODEL_PROFILE` is untouched.

## The two packages are not the same package

This has to come **before** any performance number, because it determines what the
numbers are allowed to mean. Read from the running instance (`ollama show`,
`POST /api/show`) on 2026-08-25:

| | `next` → `qwen3.8:27b` | `next-mlx` → `qwen3.8:27b-mlx` |
| --- | --- | --- |
| id | `22130167c4c2` | `5642e97495e1` |
| format | `gguf` | `safetensors` |
| family | `qwen35` | `qwen3_5` |
| parent_model | `qwen3.8:27b-q4_K_M` | *(none)* |
| quantization | **Q4_K_M** | **nvfp4** |
| parameters | 27.3B | 27.8B |
| on-disk size | 17 GB | 18 GB |
| projector | separate CLIP, 460.73M | none listed (fused) |
| packaged parameter | **`draft_num_predict 4`** | — |

Identical on both: context length 262144, embedding length 5120,
`TEMPLATE {{ .Prompt }}`, `RENDERER qwen3.8`, `PARSER qwen3.5`, `requires 0.32.12`,
capabilities `completion, vision, tools, thinking`, and every sampling parameter
(`temperature 1`, `top_k 20`, `top_p 0.95`, `min_p 0`, `presence_penalty 0`,
`repeat_penalty 1`).

Three consequences, stated once and applying to everything below:

1. **No engine claim is available.** Quantization, tensor format, parameter count,
   and projector packaging all differ. Nothing here isolates MLX. Every number is a
   comparison of two *deployable Ollama packages*.
2. **`draft_num_predict 4` is an unneutralised throughput confound**, deliberately
   so — both tags were benchmarked as shipped, which is the configuration anyone
   selecting a profile would actually get. It is speculative decoding, and it
   should bias throughput *toward* the baseline. It did not.
3. The template and sampling parity means the harness sees the same prompt contract
   either way, which is why no prompt, tool, or routing change was needed.

## What changed

One entry in `config.MODEL_PROFILES`:

```python
"next-mlx": ModelProfile("next-mlx", "qwen3.8:27b-mlx", 250, 500, 50),
```

The deadlines are **copied from `next`, not measured**, on purpose: timeout policy
must not become a second variable in the comparison.

Plus one test in `tests/test_model_profiles.py` pinning what the patch claims (the
model binds, deadlines match `next`, the host default did not move), README updates
wherever profiles are enumerated, and one new benchmark-only script,
`scripts/bench_ollama_tag.py`.

No production code beyond the dict entry. `--profile` and `--router-profile` in both
`app.py` and `evals/runner.py` derive their choices from `sorted(MODEL_PROFILES)`,
`validate_model_profiles()` iterates the whole dict, and
`tests/test_model_roles.py`'s combinatorial agent×router check picked the new
profile up with no edit. That structural claim — a new model costs one dictionary
entry — now holds for the third time (PATCH-017-01, PATCH-017-02, this one).

`evals/runner.py` was **not** changed: `duration_ms`, `model_request_ms`,
`selected_skill`, `tool_sequence`, `profile`, `router_profile`, `status`, and
`reason` were all already recorded.

### Why the benchmark is a separate script

`OllamaModel` always calls `client.chat(..., stream=True)` and sets no `options`.
The engine measurement needs the opposite (`stream=false`, `think=false`,
`temperature=0`, `num_predict=512`) so the server's counters describe one bounded,
deterministic generation. PATCH-019-01 forbids those settings from reaching runtime
configuration, so they live in `scripts/bench_ollama_tag.py` and nowhere else. It
imports only `config.OLLAMA_HOST`, is never imported by the runtime, and adds no
dependency (`urllib` + `statistics`).

## Model & parameters (provenance)

- Agent candidates: `qwen3.8:27b` (`22130167c4c2`) and `qwen3.8:27b-mlx`
  (`5642e97495e1`) — full divergence table above.
- Router, fixed for every live run: `qwen3:8b` (`500a1f067a9f`, Q4_K_M, family
  `qwen3`, 8.2B, ctx 40960), profile `fast`.
- Ollama 0.32.15, SDK `ollama` 0.6.2.
- Host: Apple M3 Max, 64 GB unified memory, macOS 25.5.0.
- Sampling in live runs: model defaults — `llm.py` still sets no `options`, so both
  packages run at their packaged `temperature 1`. This matters below.
- Both packages were confirmed `100% GPU` at `CONTEXT 262144` via `ollama ps` while
  measured. Neither ever fell back to CPU or partial offload.

## Verification

### Deterministic

```text
$ python -m pytest -q
764 passed, 29 skipped in 1.43s          # 763 before, +1 for the new profile test

$ python -m evals.runner --suite scripted
40/40 passed (0 failed)                  # profile-independent, unmoved

$ python app.py --profile huge
usage: app.py [-h] [--profile {deep,fast,mid,next,next-mlx}]
              [--router-profile {deep,fast,mid,next,next-mlx}]
app.py: error: argument --profile: invalid choice: 'huge'
        (choose from 'deep', 'fast', 'mid', 'next', 'next-mlx')

$ # role diagnostics, --profile next-mlx --router-profile fast
[model] agent next-mlx: qwen3.8:27b-mlx (request 250s, turn 500s)
[router] fast: qwen3:8b (request 120s, routing 30s)

$ # no-flag path, unchanged
[model] fast: qwen3:8b (request 120s, turn 180s, routing 30s)
```

### Compatibility gate

The first live case run on the new profile was `skill_live_sales`, chosen over
`skill_live_none` deliberately because it requires a real tool call: it proves the
MLX package's tool-calling survives the harness, not merely that text comes back.

It passed on the first attempt — `completed/final_answer`, routed to
`sales_analysis` by the model, one `sql_query` call, 26.4 s. No transport, API, or
lifecycle change was needed anywhere, so the patch's escalation trigger never
fired.

### Live A/B — router pinned to `fast`, three repetitions per profile

All runs `--router-profile fast`, agent profile varied. Result files are local
(`data/evals/`, gitignored), so the evidence is transcribed here.

`next-mlx` (`qwen3.8:27b-mlx`), 18:50–18:58:

| case | run 1 | run 2 | run 3 | median | outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| `skill_live_sales` | 13.8 s | 17.2 s | 22.3 s | **17.2 s** | 3/3 completed (+26.4 s gate run) |
| `skill_live_tracker` | 38.8 s | 25.7 s | 26.7 s | **26.7 s** | 3/3 completed |
| `skill_live_none` | 22.6 s | 4.7 s | 6.0 s | **6.0 s** | 3/3 completed |

`next` (`qwen3.8:27b`), 19:07–19:42:

| case | run 1 | run 2 | run 3 | median | outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| `skill_live_sales` | 59.7 s | 60.5 s | 186.8 s | **60.5 s** | 3/3 completed |
| `skill_live_tracker` | 73.4 s | 96.0 s | **316.4 s** | **96.0 s** | 2/3 — one `timed_out` / `model_timeout` |
| `skill_live_none` | 40.1 s | 14.8 s | 38.7 s | **38.7 s** | 3/3 completed |

Per-request medians (`model_request_ms`, agent role only — the router emits its own
event family and is excluded):

| case | `next` | `next-mlx` |
| --- | ---: | ---: |
| `skill_live_sales` | 62.9 s | 9.2 s |
| `skill_live_tracker` | 36.0 s | 16.5 s |
| `skill_live_none` | 26.7 s | 5.2 s |

**Behaviour was correct on every completed run, for both profiles.** Routing
selected `sales_analysis`, `tracker_read`, and `None` correctly every time, always
in one routing request; the tool sequences were exactly `['sql_query']`,
`['mcp_tracker__issue_get']`, and `[]`; no skill activations, no repetition-guard
trips, no forbidden tools. Tracker was live and available throughout, so no case
had to be substituted.

The single failure is `next`'s third tracker run: routing was correct, the
`mcp_tracker__issue_get` call succeeded, and then the follow-up agent decision — the
one holding the issue content in context — exceeded the profile's committed 250 s
**request** deadline, terminating the turn at 316.4 s with `model_timeout`. That is a
finding about `next`'s deadlines on this host today, not about MLX; see *Outcome*.

### Raw throughput — `scripts/bench_ollama_tag.py`, fixed prompt, 512-token cap

Throughput is the server's own `eval_count / (eval_duration / 1e9)`, not wall clock.
Medians:

| window | `next` | `next-mlx` | ratio |
| --- | ---: | ---: | ---: |
| A — early, candidate resident alone (5 runs each) | **8.36 tok/s** | **33.40 tok/s** | 4.0× |
| B — late, both 27B packages resident (3 / 5 runs) | **9.20 tok/s** | **17.56 tok/s** | 1.9× |
| C — late, candidate resident alone (3 runs) | — | **18.95 tok/s** | — |

The MLX package is faster in every window. But **its own throughput is not stable
across the session and the baseline's is** — that is the reason three windows are
reported instead of one median, and it is discussed under *Outcome*.

Window A detail, for the record:

```text
qwen3.8:27b-mlx  37.74 / 36.18 / 33.40 / 32.95 / 33.05 tok/s   (13.6-15.5 s per 512 tok)
qwen3.8:27b       7.78 /  8.18 /  8.36 /  8.45 /  8.51 tok/s   (60.2-65.8 s per 512 tok)
```

### Cold load

One controlled cold request per tag: `ollama stop <tag>`, absence confirmed with
`ollama ps`, then one request.

| | `next` | `next-mlx` |
| --- | ---: | ---: |
| `load_duration` | 6.67 s | **2.52 s** |
| first-generation rate | 7.20 tok/s | 12.22 tok/s |
| warm rate in the same window | 8.36 tok/s | 33.40 tok/s |

Worth naming: `load_duration` understates the real cost of a cold model. Both
packages generate measurably slower on the request that follows a load — the MLX
package especially so (12.2 against 33.4 tok/s, a third of warm). Ollama attributes
only weight loading to `load_duration`; whatever else settles afterwards lands in
`eval_duration` and is invisible to that field.

## Outcome

**Verdict: `keep experimental`.**

The candidate won every measurement taken. Warm throughput 1.9–4.0× higher depending
on window; end-to-end live medians 2.2–6.4× faster per case; per-agent-request
medians 2.2–6.8× faster; cold `load_duration` 2.6× lower. It passed 10/10 live cases
with correct routing, correct tool sequences, and correct termination, against the
baseline's 8/9 — and it did so at deadlines copied from a profile calibrated for the
slower package, so it never came close to a timeout. Tool calling, thinking, and the
262144 context all work through it unchanged, and no harness change was required.

That is a real result, and it is still not enough to adopt. Three reasons, all of
which are about the quality of the evidence rather than the candidate:

**The comparison cannot attribute the win.** Quantization (nvfp4 vs Q4_K_M),
parameter count, tensor format, and projector packaging all differ. `nvfp4` at 27.8B
against `Q4_K_M` at 27.3B is a different numerical footprint, and the honest reading
is "this package is faster on this host", full stop. Notably the baseline carries
`draft_num_predict 4` — speculative decoding, which should help *it* — and it lost
anyway; that makes the gap more striking, not better explained.

**The candidate's throughput is not stable and the cause is not established.** It
measured 33.40 tok/s early in the session and 17.5–19.0 tok/s in three separate
later blocks, and it did not recover when the other 27B package was evicted and it
was left resident alone (window C, 18.95 tok/s). The baseline held 8.36 → 9.20 tok/s
across the same span. What was ruled out: no thermal or performance warning was
recorded (`pmset -g therm`), memory was 36% free with both models nominally fitting
64 GB, `ollama ps` reported `100% GPU` at ctx 262144 throughout, and each eval case
runs in a fresh process with fresh history. What remains unexplained is why a
package that starts at 33 tok/s settles near 18 and stays there. A runtime whose
throughput halves for unknown reasons is not one to make a default on three
sessions' evidence.

**The baseline degraded during the session in a way that distorts its own numbers.**
`next`'s `skill_live_sales` went 59.7 → 60.5 → 186.8 s across repetitions, and a
fourth run of the same case reached 394.6 s. Its raw throughput was flat (8.4–9.2
tok/s) across exactly that window, so the growth is in *how many tokens it
generated*, not how fast — consistent with unpinned sampling at `temperature 1` and
long thinking chains, which PATCH-017-02 already flagged as a confound for any
cross-profile quality comparison. The medians above use three repetitions as the
patch requires, but the spread on `next` is wide enough that its medians should be
read as "this host, this session", not as a property of the model.

So the profile stays in `main`, selectable, marked experimental, at `next`'s
deadlines — and no default moves. What this entry establishes is that the MLX-backed
package is compatible, materially faster here, and worth a proper adoption decision.
What it does not establish is *why*, or that the advantage is stable.

One acceptance criterion was met by circumstance rather than by action: the tag was
already pulled on this host before the patch began, so `ollama pull qwen3.8:27b-mlx`
was never exercised. Serving is proven; pulling is assumed.

## Follow-ups

- **`next`'s committed deadlines produced a real `model_timeout` today.** PATCH-017-02
  calibrated 250/500/50 against a worst observed request of 84.3 s; this session saw
  a 214.4 s request complete and a longer one time out. Either the host changed or
  those numbers were always calibrated on too few samples. This deserves its own
  PATCH against SPEC-017 — deliberately not folded in here, since PATCH-019-01 is
  forbidden from touching `next`.
- **The MLX package's throughput decay needs a cause** before adoption. A session
  that benchmarks a single tag repeatedly over an hour, with `ollama ps` and
  `load_duration` sampled between blocks, would separate "degrades with uptime" from
  "degrades after eviction/reload".
- **Unpinned sampling now materially distorts profile comparison**, not just quality
  claims: `temperature 1` plus thinking gives `next` a 6.6× spread on one live case.
  Pinning sampling per profile is still a SPEC, and it is now better motivated.
- **Adoption, if pursued, needs a quality comparison, not a latency one.** Every case
  here is a smoke test that passes or fails; none scores answer quality, and nvfp4 vs
  Q4_K_M is exactly the kind of difference that would show up there first.
- `load_duration` alone is a poor cold-cost metric (see above). If cold behaviour ever
  drives a decision, the first warm request after load should be recorded too.
