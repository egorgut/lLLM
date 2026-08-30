"""Measure what real turns actually spend of the per-turn tool-call budget (SPEC-021 §7.1).

Usage:

    python scripts/analyze_turn_budget.py                 # data/traces
    python scripts/analyze_turn_budget.py --traces DIR    # another trace directory
    python scripts/analyze_turn_budget.py --percentile 95

SPEC-010 set ``MAX_TOOL_CALLS_PER_TURN`` with no derivation, and SPEC-021 exists
because nobody could re-derive it. This script is the re-derivation: it reads the
local trace history and reports the distribution of ``tool_calls_executed`` over
completed turns, split by model profile and by whether a skill was active.

The traces themselves are **not** committed — ``data/traces/`` is git-ignored
(config.py, SPEC-011) — so this script plus the numbers transcribed into
``docs/journal/SPEC-021-turn-budget-revision.md`` are the reproducible artifact,
not the data. SPEC-021 §7.1 calls the history "committed"; it is not.

Two things this measurement cannot do on its own, both recorded here so a reader
does not over-trust the output:

* The distribution is **right-censored** at the budget being revised. No completed
  turn can report more calls than the limit allowed, so a percentile taken over
  history alone systematically under-reads. §7.1 step 2's raised-budget scenario
  corpus is what removes the censoring; this script measures that corpus too, by
  pointing ``--traces`` at the runs made under the raised value.
* A turn that spent a call on ``activate_skill`` reports it in
  ``tool_calls_executed`` for every run recorded before SPEC-021. Pass
  ``--work-only`` to subtract ``skill_activations`` and read the distribution in
  the post-SPEC-021 accounting, where an activation is not work.

The trace schema is additive and ``schema_version`` stayed at 1 throughout, so
every field below is read defensively: ``model_profile`` is absent before
SPEC-017 (``model_name`` is the fallback), ``selected_skill`` before SPEC-012,
and ``skill_activations`` before SPEC-018.

This module never calls Ollama, touches chat history, or accesses the network.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Allow running as a plain script by making the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MAX_TOOL_CALLS_PER_TURN, PROJECT_ROOT, TRACE_DIR  # noqa: E402


def _percentile(counts: Counter, fraction: float) -> int | None:
    """The smallest value whose cumulative share reaches `fraction`.

    Deliberately the nearest-rank definition on a discrete counter rather than an
    interpolating one: the quantity is "how many tool calls", which has no
    meaningful value between 3 and 4, and a budget must be an integer anyway.
    """

    total = sum(counts.values())
    if not total:
        return None
    threshold = fraction * total
    seen = 0
    for value in sorted(counts):
        seen += counts[value]
        if seen >= threshold:
            return value
    return max(counts)


def _read_turns(directory: Path, *, work_only: bool) -> list[dict]:
    """Every `turn_finished` event, each joined to its own run's profile.

    The join is on `run_id`, not on the file, because the trace layout changed:
    PATCH-011-01 moved from one shared `agent.jsonl` to one file per run, and the
    legacy file interleaves several runs. Reading the profile per file would
    attribute every turn in it to whichever run happened to start last.

    A malformed line is skipped rather than fatal: the history spans months of
    runs, and one truncated file must not make the whole measurement impossible.
    """

    profiles: dict[str, str] = {}
    events: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            name = event.get("event")
            if name == "run_started":
                # `model_profile` only exists from SPEC-017 onwards; before that a
                # run is identified by the model it ran, which is what a profile
                # names anyway.
                profile = event.get("model_profile") or event.get("model_name")
                if profile:
                    profiles[event.get("run_id")] = profile
            elif name == "turn_finished":
                events.append(event)

    turns: list[dict] = []
    for event in events:
        calls = event.get("tool_calls_executed")
        if not isinstance(calls, int):
            continue
        if work_only:
            calls = max(0, calls - int(event.get("skill_activations") or 0))
        turns.append(
            {
                "profile": profiles.get(event.get("run_id"), "(unknown)"),
                "status": event.get("status"),
                "reason": event.get("reason"),
                "calls": calls,
                "skill": bool(event.get("selected_skill")),
                "final_text_chars": event.get("final_text_chars"),
            }
        )
    return turns


def _distribution_row(label: str, counts: Counter, percentile: float) -> str:
    total = sum(counts.values())
    if not total:
        return f"{label:<28} n=   0"
    seen = 0
    cells = []
    for value in sorted(counts):
        seen += counts[value]
        cells.append(f"{value}:{counts[value]} ({100 * seen / total:.0f}%)")
    p = _percentile(counts, percentile / 100)
    return f"{label:<28} n={total:4d}  p{percentile:g}={p}  max={max(counts)}  " + " ".join(cells)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Distribution of tool calls per turn, from local trace history (SPEC-021 §7.1).",
    )
    parser.add_argument(
        "--traces",
        default=str(PROJECT_ROOT / TRACE_DIR),
        help="Directory of JSONL trace files (default: data/traces).",
    )
    parser.add_argument(
        "--percentile",
        type=float,
        default=95.0,
        help="Percentile to report for each population (default: 95).",
    )
    parser.add_argument(
        "--work-only",
        action="store_true",
        help="Subtract skill activations, i.e. read the distribution in SPEC-021 accounting.",
    )
    args = parser.parse_args()

    directory = Path(args.traces)
    if not directory.is_dir():
        print(f"No trace directory at {directory}.", file=sys.stderr)
        return 1

    turns = _read_turns(directory, work_only=args.work_only)
    if not turns:
        print(f"No turn_finished events found in {directory}.", file=sys.stderr)
        return 1

    completed = [turn for turn in turns if turn["status"] == "completed"]
    pct = args.percentile

    print(f"trace directory:   {directory}")
    print(f"configured budget: MAX_TOOL_CALLS_PER_TURN = {MAX_TOOL_CALLS_PER_TURN}")
    print(f"accounting:        {'work calls only (activations excluded)' if args.work_only else 'every tool call (as recorded)'}")
    print(f"turns:             {len(turns)} total, {len(completed)} completed")
    print()

    print("== completed turns, tool_calls_executed ==")
    overall = Counter(turn["calls"] for turn in completed)
    print(_distribution_row("all", overall, pct))
    print()

    print("== by skill ==")
    for label, active in (("skill active", True), ("no skill", False)):
        counts = Counter(t["calls"] for t in completed if t["skill"] is active)
        print(_distribution_row(label, counts, pct))
    print()

    print("== by profile ==")
    by_profile: dict[str, Counter] = defaultdict(Counter)
    for turn in completed:
        by_profile[turn["profile"]][turn["calls"]] += 1
    for profile in sorted(by_profile):
        print(_distribution_row(profile, by_profile[profile], pct))
    print()

    print("== turns that did not complete ==")
    outcomes = Counter((t["status"], t["reason"]) for t in turns if t["status"] != "completed")
    if not outcomes:
        print("(none)")
    for (status, reason), count in outcomes.most_common():
        print(f"  {status}/{reason}: {count}")

    # The censoring the header warns about, stated as a number rather than left
    # for the reader to notice: these are the turns the budget itself truncated.
    exhausted = [t for t in turns if t["reason"] in ("tool_call_limit", "budget_exhausted")]
    if exhausted:
        print()
        print(
            f"NOTE: {len(exhausted)} turn(s) reached the budget, so this distribution is "
            f"right-censored at {max(t['calls'] for t in exhausted)}."
        )
        silent = [t for t in exhausted if not t.get("final_text_chars")]
        if silent:
            print(f"      {len(silent)} of them delivered no text to the user at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
