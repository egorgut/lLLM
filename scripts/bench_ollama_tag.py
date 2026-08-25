"""A fixed warm/cold throughput benchmark for one Ollama tag (PATCH-019-01).

Usage:

    python scripts/bench_ollama_tag.py --model qwen3.8:27b     --runs 5
    python scripts/bench_ollama_tag.py --model qwen3.8:27b-mlx --runs 5

    ollama stop qwen3.8:27b-mlx && ollama ps      # confirm it is not resident
    python scripts/bench_ollama_tag.py --model qwen3.8:27b-mlx --runs 1  # cold

This is **not** part of `lLLM`. It measures the inference service directly so
that a package comparison is not filtered through the agent loop, the router,
prompts, tools, or history. It is never imported by the runtime.

It deliberately does not reuse `llm.py`. `OllamaModel` always calls
`client.chat(..., stream=True)` and sets no `options`; the benchmark needs the
opposite (`stream=false`, `think=false`, `temperature=0`, `num_predict=512`) so
that server-side counters describe one bounded, deterministic generation.
PATCH-019-01 forbids those settings from reaching normal runtime configuration,
so they live here and nowhere else.

Everything else is left **as the package ships it**. In particular
`qwen3.8:27b` carries `PARAMETER draft_num_predict 4` (speculative decoding) and
`qwen3.8:27b-mlx` does not; that is not overridden here. The measurement answers
"which deployable package generates faster on this host", not "is the MLX engine
faster", and the difference has to be stated wherever the numbers are quoted.

Throughput comes from the server's own counters, not from wall clock:

    tokens_per_second = eval_count / (eval_duration / 1e9)

Standard library only, no new dependency.
"""

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as a plain script (python scripts/bench_ollama_tag.py) by making
# the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import OLLAMA_HOST  # noqa: E402

# Committed and fixed, so that two tags answer the same question. Long enough to
# reach the 512-token cap on both packages, and phrased to need no tool, no
# retrieval, and no context beyond itself.
FIXED_PROMPT = (
    "Explain, in plain prose and without lists or code, how a bounded agent loop "
    "decides when to stop: what it does with each tool result, why a call budget "
    "matters, and how it distinguishes a final answer from another tool call. "
    "Write roughly four hundred words."
)

# Benchmark-only. These must not appear in `llm.py` or any runtime path.
BENCHMARK_OPTIONS = {"temperature": 0, "num_predict": 512}

# Server-reported fields recorded verbatim for every run, in nanoseconds except
# the token counts.
COUNTERS = (
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
    "load_duration",
    "total_duration",
)


def run_once(model: str, timeout: float) -> dict:
    """One non-streaming generation, returning the server's own counters."""

    body = json.dumps(
        {
            "model": model,
            "prompt": FIXED_PROMPT,
            "stream": False,
            "think": False,
            "options": BENCHMARK_OPTIONS,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    return {name: payload.get(name, 0) for name in COUNTERS}


def tokens_per_second(run: dict) -> float:
    """Generation throughput from the server's counters; 0.0 if it emitted nothing."""

    eval_duration = run.get("eval_duration") or 0
    if not eval_duration:
        return 0.0
    return run["eval_count"] / (eval_duration / 1e9)


def format_run(index: int, run: dict) -> str:
    return (
        f"run {index}: "
        f"{tokens_per_second(run):7.2f} tok/s  "
        f"eval {run['eval_count']:4d} tok / {run['eval_duration'] / 1e9:7.2f}s  "
        f"prompt {run['prompt_eval_count']:4d} tok / "
        f"{run['prompt_eval_duration'] / 1e9:6.2f}s  "
        f"load {run['load_duration'] / 1e9:7.2f}s  "
        f"total {run['total_duration'] / 1e9:7.2f}s"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark one Ollama tag with fixed, benchmark-only settings."
    )
    parser.add_argument("--model", required=True, help="Ollama tag, e.g. qwen3.8:27b-mlx")
    parser.add_argument("--runs", type=int, default=5, help="Requests to issue (default 5).")
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Per-request timeout in seconds; generous, because a cold run loads the model.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit every run and the medians as JSON, for transcription into the journal.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2

    runs = []
    for index in range(1, args.runs + 1):
        try:
            run = run_once(args.model, args.timeout)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace").strip()
            print(f"{args.model}: HTTP {error.code} {detail}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"{args.model}: request failed: {error}", file=sys.stderr)
            return 1
        runs.append(run)
        if not args.json:
            print(format_run(index, run))

    rates = [tokens_per_second(run) for run in runs]
    medians = {
        "tokens_per_second": statistics.median(rates),
        **{f"{name}_median": statistics.median(run[name] for run in runs) for name in COUNTERS},
    }

    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "options": BENCHMARK_OPTIONS,
                    "runs": [{**run, "tokens_per_second": rate} for run, rate in zip(runs, rates)],
                    "median": medians,
                },
                indent=2,
            )
        )
    else:
        print(
            f"\n{args.model}: median {medians['tokens_per_second']:.2f} tok/s over "
            f"{len(runs)} run(s); median total "
            f"{medians['total_duration_median'] / 1e9:.2f}s, median load "
            f"{medians['load_duration_median'] / 1e9:.2f}s"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
