import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

OLLAMA_HOST = "http://localhost:11434"


# Model profiles (SPEC-017). A model is not a name on its own: the deadlines a
# turn needs depend on how fast that model decides, so the selectable unit is
# one bundle of "which model" plus "how long its decisions may take". Measured
# warm on an Apple M3 Max (both models Q4_K_M): a routing response takes ~3.6 s
# on qwen3:8b against ~11.3 s on qwen3:32b, and one tool-emitting agent decision
# ~11 s against ~40-47 s. Reusing the 8B deadlines for a 32B model would turn
# ordinary latency into spurious turn_timed_out outcomes.
#
# Everything here is host-owned (SPEC-011 §10): the model never sees, supplies,
# or influences a profile, and the active profile is chosen once per run.
@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    model_request_timeout_seconds: float
    agent_turn_timeout_seconds: float
    skill_routing_timeout_seconds: float


MODEL_PROFILES = {
    # The project's original numbers, unchanged: every journal from SPEC-005
    # onwards was recorded under exactly this profile and stays reproducible.
    "fast": ModelProfile("fast", "qwen3:8b", 120, 180, 30),
    # PATCH-017-01. Deadlines interpolated from the two measured profiles by
    # parameter count (14.8B sits 0.27 of the way from 8.2B to 32.8B), not
    # measured: ~5.7 s routing and ~20 s per tool-emitting decision, carrying
    # the same headroom ratio as its neighbours. A live turn_timed_out here is
    # a signal to remeasure, not a model verdict.
    "mid": ModelProfile("mid", "qwen3:14b", 180, 300, 40),
    # Proposed from the measurements above with headroom for a four-call turn.
    "deep": ModelProfile("deep", "qwen3:32b", 300, 600, 60),
    # PATCH-017-02. A different model generation (family `qwen35`, ctx 262144),
    # so it is deliberately not slotted into the fast/mid/deep size scale: it is
    # a candidate baseline, not a rung. Unlike `mid`, these deadlines are
    # *measured* on this host, warm: routing 3.6-4.9 s, a tool-emitting decision
    # 26-33 s, and one 65 s final answer -- faster to route than qwen3:32b and
    # faster to decide, despite sitting between `mid` and `deep` by parameter
    # count. Carrying the same ~2.5-3x margin the other profiles apply to a
    # four-call turn (4.3 + 4x33 + 65 ~= 201 s) gives 500 s.
    "next": ModelProfile("next", "qwen3.8:27b", 250, 500, 50),
    # PATCH-019-01. Experimental, and neither a rung nor a candidate default: the
    # same model family as `next` shipped as a *different Ollama package* --
    # safetensors / nvfp4 / 27.8B with a fused projector, against `next`'s gguf /
    # Q4_K_M / 27.3B plus a separate CLIP projector and a packaged
    # `draft_num_predict 4`. Anything measured between the two is therefore a
    # comparison of two deployable packages, never of an inference engine alone.
    # The deadlines are *copied* from `next` deliberately and are not measured for
    # this package, so timeout policy does not become a second variable.
    "next-mlx": ModelProfile("next-mlx", "qwen3.8:27b-mlx", 250, 500, 50),
}
DEFAULT_MODEL_PROFILE = "fast"


def resolve_model_profile(name: str | None = None) -> ModelProfile:
    """The profile to run under; `None` selects the host default.

    An unknown name is a deployment mistake, not a turn-time event, so this
    raises with the valid names rather than falling back to a default that
    silently runs the wrong model.
    """

    if name is None:
        name = DEFAULT_MODEL_PROFILE
    try:
        return MODEL_PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(MODEL_PROFILES))
        raise ValueError(f"Unknown model profile '{name}'. Valid profiles: {valid}.") from None


# Component-specific model roles (SPEC-019). One run resolves two of the profiles
# above: the one the agent loop decides with, and the one skill routing classifies
# with. They are the same object unless the host asks for a split, which is why
# the historical single-profile path is preserved exactly rather than emulated.
#
# This is deliberately not a second profile type and not a generic
# "role -> profile" registry (SPEC-019 §5): it names the two model call sites the
# runtime actually has -- `SkillRouter.route` and `AgentRunner.respond` -- and
# nothing else. Like a profile, it is host-owned and fixed for the whole run.
@dataclass(frozen=True)
class ModelRoles:
    agent: ModelProfile
    router: ModelProfile

    @property
    def split(self) -> bool:
        """Whether the two roles resolved to different profiles.

        Identity, not equality: `MODEL_PROFILES` hands out the same frozen object
        for a given name, so `--profile next --router-profile next` collapses back
        to one role and one transport.
        """

        return self.router is not self.agent


def resolve_model_roles(agent: str | None = None, router: str | None = None) -> ModelRoles:
    """The two roles this run runs under; `None` router means "same as the agent".

    `--profile` stays authoritative for the agent role, so omitting the router
    override reproduces SPEC-017 behavior exactly. Both names go through
    `resolve_model_profile`, so an unknown one fails here, before the chat loop.
    """

    agent_profile = resolve_model_profile(agent)
    return ModelRoles(
        agent=agent_profile,
        router=agent_profile if router is None else resolve_model_profile(router),
    )


# Agent-side reasoning mode (SPEC-020). A thinking-capable model emits hidden
# reasoning before its answer, and how much of it is worth paying for is a
# question about the run, not about the model file -- so the mode is host-owned
# and fixed for the whole process, exactly like a profile: the model never sees
# it, never supplies it, and cannot change it mid-session.
#
# `auto` is the backward-compatible default: it sends no `think` at all, leaving
# the model/server default exactly as every journal before SPEC-020 recorded it.
#
# There is deliberately no `high`/`xhigh` here (SPEC-020 §4.1). The installed
# Ollama SDK (0.6.2) types `think` as bool | Literal["low", "medium", "high"],
# while the Qwen3.8 template's highest named effort is `xhigh` -- two different
# names for "the most deliberation this thing does". `auto` already preserves
# whatever the package considers its default, without inventing a mapping
# between the two vocabularies. If a later Ollama exposes a stable native
# contract for that tier, adding it is a separate PATCH.
ReasoningMode = Literal["auto", "off", "low", "medium"]
REASONING_MODES: tuple[str, ...] = ("auto", "off", "low", "medium")
DEFAULT_REASONING_MODE = "auto"


def resolve_reasoning_think(mode: str | None = None) -> bool | str | None:
    """The Ollama `think` value one reasoning mode asks for; `None` selects the default.

    Returns what the transport passes straight through to the SDK:

        auto   -> None    (send nothing; the model/server default stands)
        off    -> False
        low    -> "low"
        medium -> "medium"

    An unknown name is a deployment mistake, not a turn-time event, so this
    raises with the valid names -- the same shape and the same timing as
    `resolve_model_profile` above, and for the same reason: it must fail before
    the chat loop rather than silently run under the wrong policy.
    """

    if mode is None:
        mode = DEFAULT_REASONING_MODE
    if mode not in REASONING_MODES:
        valid = ", ".join(REASONING_MODES)
        raise ValueError(f"Unknown reasoning mode '{mode}'. Valid modes: {valid}.")
    if mode == "auto":
        return None
    if mode == "off":
        return False
    return mode


# Maximum number of stored messages sent to the model on each turn.
# The full history is persisted on disk; only this window reaches the LLM.
MAX_CONTEXT_MESSAGES = 20

# Where the persistent conversation history is stored.
CHAT_HISTORY_PATH = "data/chat_history.json"

# Cross-turn agent action provenance (PATCH-010-05). A completed turn's tool
# executions leave a bounded, session-only receipt so the next turn can still see
# *that* a tool ran, without any raw tool payload entering semantic memory.
# Deliberately separate from TRACE_PAYLOAD_PREVIEW_CHARS below: what the model
# remembers must not depend on whether tracing happens to be enabled.
ACTION_RECEIPT_ARGUMENT_MAX_CHARS = 500
MAX_ACTION_RECEIPTS_IN_CONTEXT = 8

# Bounded agent loop (SPEC-010), re-derived from measurement in SPEC-021. The
# maximum number of *work* tool executions the model may drive within a single
# user turn. Host-owned: the model can never read or change it, and a request
# beyond this count is not executed. Since SPEC-021 exceeding it is no longer
# terminal — the turn makes one final, tool-less request and answers with what it
# has (agent.py, TOOL_BUDGET_EXHAUSTED_POLICY).
#
# SPEC-010 set this to 4 with no derivation, which is why SPEC-021 exists. The
# rule, stated before the data was read, is recorded in
# docs/journal/SPEC-021-turn-budget-revision.md:
#
#   the budget covers the 95th percentile of tool_calls_executed over completed
#   turns, on the heavier subpopulation (skill-active turns), rounded up; where
#   history is right-censored at the current limit, an uncensored scenario
#   corpus is authoritative.
#
# Re-run it with `python scripts/analyze_turn_budget.py`. Measured over 221
# completed turns of trace history plus a scenario corpus run at a temporarily
# raised budget of 12, the rule yields **4** — the value already in place. The
# number was not what was broken. What was broken was what it counted (SPEC-018
# charged skill activations against it, so a two-phase request spent 2 of its 4
# calls on orchestration and died before doing the work) and what happened when
# it ran out (the user received nothing). SPEC-021 fixes both and leaves the
# number alone rather than raising it on a hunch: see MAX_SKILL_ACTIVATIONS_PER_TURN
# below, and note that raising this constant raises worst-case model requests,
# latency and accumulated tool payload in direct proportion.
MAX_TOOL_CALLS_PER_TURN = 4

# Agent reliability (SPEC-011). Host-owned time limits and repeated-call policy.
# None of these are ever supplied or changed by the model. Timeouts are
# caller-side deadlines (see reliability.run_with_deadline): a component that
# does not return in time is abandoned, not forcibly terminated.
#
# The model-latency deadlines (request, whole turn, routing) moved into
# ModelProfile above (SPEC-017), because their correct value depends on the
# model. What stays *here* bounds host work, which no model choice changes —
# with one correction SPEC-021 §2.4 had to make: that classification was applied
# to MAX_TOOL_CALLS_PER_TURN too, and for that constant it is false. The loop
# continues through exactly one path, an executed tool call, so the budget is
# also the bound on model requests per turn, and therefore on worst-case latency
# and on how much tool payload accumulates in the final request. It is measured
# and justified above rather than treated as a pure host-side counter.
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
MAX_IDENTICAL_TOOL_CALLS = 2

# Local structured tracing (SPEC-011). Append-only JSONL, local-only, never
# uploaded. One file per run — data/traces/agent-<run_id>.jsonl, built by
# tracing.trace_file_path (PATCH-011-01) — rather than one shared growing
# file. Generated traces are git-ignored; only this configuration and the
# tracing code are committed.
TRACE_ENABLED = True
TRACE_DIR = "data/traces"
TRACE_PAYLOAD_PREVIEW_CHARS = 1000

# Where MCP child processes' own stderr goes (PATCH-009-01). Every MCP server is
# a child process speaking stdio, and the SDK's `stdio_client` defaults its
# `errlog` to this process's stderr -- i.e. straight onto the user's terminal,
# where the SDK's per-request `INFO` logging collides with the CLI's own output
# and with the activity indicator. One file per run, like traces (PATCH-011-01),
# generated and git-ignored.
#
# Redirected rather than discarded on purpose: SPEC-009 keeps startup errors
# anonymous ("The MCP server could not be started."), so this stream is the only
# place a failing child explains itself -- PATCH-013-01 was diagnosed from
# exactly such a traceback.
MCP_LOG_DIR = "data/mcp"

# External Yandex Tracker MCP server (SPEC-013). Disabled by default; a fresh
# checkout never needs Tracker credentials. Enabling requires TRACKER_TOKEN and
# exactly one organisation-id variable (TRACKER_CLOUD_ORG_ID or
# TRACKER_ORG_ID); see mcp_integration/config.py, which is the only module
# that reads that environment. The package reference is pinned to an exact
# tested release; committed configuration must never use "@latest".
TRACKER_MCP_SERVER_ID = "tracker"
TRACKER_MCP_COMMAND = "uvx"
TRACKER_MCP_PACKAGE = "yandex-tracker-mcp==0.7.2"
# The MCP SDK inside the uvx child environment is bounded to the same range as
# this project's own requirements.txt (PATCH-013-01). Pinning the server package
# alone was not enough: 0.7.2 declares `mcp[cli]>=1.21` with no upper bound, and
# `uvx` resolves the child independently, so the SDK's 2.0 release — which
# removed the `mcp.server.fastmcp` module the server imports — silently entered
# the environment and crashed the child at startup. An external integration is
# only reproducible if its transitive dependencies are bounded too.
TRACKER_MCP_SDK_REQUIREMENT = "mcp>=1.27,<2"
TRACKER_MCP_ARGS = [
    "--from",
    TRACKER_MCP_PACKAGE,
    "--with",
    TRACKER_MCP_SDK_REQUIREMENT,
    "yandex-tracker-mcp",
]
TRACKER_MCP_REQUIRED_TOOLS = frozenset(
    {
        "issue_get",
        "issues_find",
        "queue_get_metadata",
        "issue_get_comments",
    }
)
# Advisory guidance surfaced to the model via the tracker_read skill
# instructions, not mechanically enforced on tool arguments (enforcing a
# per-argument cap would be Yandex-specific dispatch logic, which this project
# deliberately avoids). The actual host-owned backstop against an oversized
# result is MCP_RESULT_MAX_CHARS below, applied generically to every MCP call.
TRACKER_MAX_SEARCH_PAGE_SIZE = 50

# Generic bound on any single MCP tool result (SPEC-013 §14, "Bound external
# data at several layers"). Applies to every MCP server, not just Tracker: a
# third-party response must never be assumed small merely because the
# request asked for fewer records or fields.
MCP_RESULT_MAX_CHARS = 20_000

# Terminal rendering of a tool call and its result (PATCH-010-02, tool_render.py).
# A different concern from MCP_RESULT_MAX_CHARS above: that one is the model's
# context budget and bounds what the tool returns, these bound only what the
# screen shows. The width is a constant rather than the real terminal size on
# purpose -- the rendering must be identical on a TTY and through a pipe, so the
# CLI transcripts committed to README.md stay reproducible.
TOOL_DISPLAY_WIDTH = 100
# Enough that the largest routine local result -- a sandbox_execute run with one
# published artifact -- still fits whole, while anything unbounded gets cut.
TOOL_RESULT_PREVIEW_LINES = 16

# Local Chinook SQLite database (SPEC-008). Resolved relative to this file so the
# paths hold regardless of the current working directory. The seed script is the
# trusted source under version control; the runtime database is generated from it
# by scripts/init_database.py and is not committed.
PROJECT_ROOT = Path(__file__).resolve().parent
CHINOOK_SEED_PATH = PROJECT_ROOT / "data" / "seed" / "Chinook_Sqlite.sql"
SQLITE_DATABASE_PATH = PROJECT_ROOT / "data" / "chinook.sqlite"

# Filesystem-backed skill layer (SPEC-012). Skills are declarative packages that
# live under SKILLS_ROOT and are discovered, validated, and frozen at startup —
# never supplied or mutated by the model. The routing model sees only a compact
# catalog (name + description); the full instruction of the one selected skill is
# loaded lazily for that turn. All bounds below are host-owned and validated at
# startup (skill_runtime.config_validation.validate_skill_config).
SKILLS_ROOT = PROJECT_ROOT / "skills"
# Skill routing has its own component timeout (ModelProfile.skill_routing_timeout_seconds,
# SPEC-017) but still counts against the whole-turn budget shared with agent execution.
# Additional routing requests permitted after the first; 1 => at most two total.
SKILL_ROUTING_REPAIR_ATTEMPTS = 1
MAX_SKILL_ROUTING_RESPONSE_CHARS = 2_000
MAX_SKILL_INSTRUCTION_CHARS = 20_000
MAX_SKILL_SCHEMA_BYTES = 100_000
MAX_SKILLS = 100
MAX_SKILL_DESCRIPTION_CHARS = 200

# Mid-turn skill activation (SPEC-018). How many times one turn may swap its
# active skill through the host-owned `activate_skill` tool; the router's own
# initial selection does not count against it. Exceeding this limit is
# recoverable — the turn continues under the skill already active.
#
# SPEC-018 additionally charged an activation against MAX_TOOL_CALLS_PER_TURN,
# reasoning that hiding it would let a thrashing model run unbounded. SPEC-021
# §4.4 keeps the concern and drops the charge: an activation is orchestration,
# not work, and taxing the work budget made a turn's capacity depend on how well
# the router happened to guess (PATCH-018-02 run 5 died of exactly that).
#
# What bounds a thrashing model instead is the agent loop's own control-call
# budget, which it derives as this value **+ 1**. The +1 is not slack: this
# counter is *not* incremented when an attempt is refused (see
# skill_runtime/activation.py), so bounding attempts by this number alone would
# leave the handler's recoverable `activation_limit` answer unreachable. The
# loop's closed-form bound on model requests per turn is written out in
# agent.py, beside the counters it is built from.
MAX_SKILL_ACTIVATIONS_PER_TURN = 2

# Host baseline tools (PATCH-012-02). Tools that stay in the model-facing tool
# set while a skill is active, composed with — never replaced by — the skill's
# own `allowed_tools`. A skill narrows *domain* capability; it must not erase a
# safe, general-purpose host utility that the domain task may legitimately need
# (a Tracker deadline is only "overdue" relative to the current time).
#
# The host alone owns this list. It is fixed here, deterministic in order, and
# deliberately minimal: a skill cannot add to it, remove from it, or grant itself
# anything through it, and it is not configurable by the model or through chat.
# Baseline status is never inferred from a tool's name, its MCP server, its
# description, or the fact that it happens to be read-only — adding a member is
# a repository change that goes through review. The names are validated against
# the final tool registry after MCP registration
# (skill_runtime.config_validation.validate_baseline_tools), because the MCP
# tools do not exist yet at import time.
BASELINE_TOOL_NAMES: tuple[str, ...] = ("mcp_time__get_current_time",)

# Isolated sandbox runtime (SPEC-015). The runtime itself owns every execution
# rule below; SPEC-016 added the model-facing boundary on top of it (see the
# SANDBOX_TOOL_* block after this one) without changing a single limit here.
#
# Every value below is host-owned. A SandboxJob carries only the language, the
# source, and optional input files; it has no field that could raise a limit,
# change the image, add an environment variable, or alter a mount. The
# configured tag is resolved to an immutable image ID before any container is
# created, so a retagged image cannot silently change what runs.
SANDBOX_IMAGE_REF = "lllm-sandbox:spec-015"

# Wall-clock bounds. execution + cleanup must stay strictly below
# TOOL_EXECUTION_TIMEOUT_SECONDS above (10 + 5 < 30), so that when SPEC-016
# calls this runtime from one ordinary tool execution, the sandbox can kill and
# remove its own container before the outer caller-side deadline fires.
SANDBOX_EXECUTION_TIMEOUT_SECONDS = 10
SANDBOX_DOCKER_CONTROL_TIMEOUT_SECONDS = 10
SANDBOX_CLEANUP_TIMEOUT_SECONDS = 5

# Container resource ceilings, enforced by Docker rather than by Python: unlike
# reliability.run_with_deadline, exceeding these terminates the whole container
# and every process in it.
SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024
SANDBOX_CPUS = 1.0
SANDBOX_PIDS_LIMIT = 64
SANDBOX_NOFILE_LIMIT = 64

# The only writable locations in the container. The root filesystem is mounted
# read-only and there is no writable host bind mount, so a job cannot consume
# host disk: generated files live in this bounded tmpfs and vanish with the
# container.
SANDBOX_TMP_BYTES = 16 * 1024 * 1024
SANDBOX_OUTPUT_TMPFS_BYTES = 8 * 1024 * 1024

# Bounds on what the host puts into a container.
SANDBOX_MAX_SOURCE_BYTES = 100_000
SANDBOX_MAX_INPUT_FILES = 20
SANDBOX_MAX_INPUT_FILE_BYTES = 1_000_000
SANDBOX_MAX_INPUT_TOTAL_BYTES = 2_000_000

# Bounds on what crosses back out. Output is streamed and counted incrementally;
# crossing either limit kills the container rather than buffering more.
SANDBOX_MAX_STDOUT_BYTES = 100_000
SANDBOX_MAX_STDERR_BYTES = 100_000

SANDBOX_MAX_ARTIFACT_FILES = 20
SANDBOX_MAX_ARTIFACT_FILE_BYTES = 2_000_000
SANDBOX_MAX_ARTIFACT_TOTAL_BYTES = 5_000_000
SANDBOX_MAX_ARTIFACT_PATH_CHARS = 240

# Private per-job scratch space for read-only source/input mounts and the
# collected output copy. Every job directory is deleted in a finally block; the
# whole tree is git-ignored and never holds secrets.
SANDBOX_TEMP_ROOT = PROJECT_ROOT / "data" / "sandbox" / "tmp"

# Model-facing sandbox boundary (SPEC-016). Deliberately three values: the tool
# layer adds an artifact destination and a time margin, and reuses every SPEC-015
# limit above rather than maintaining a second set that could drift wider.
#
# There is no workspace root here on purpose. SPEC-015 already creates, mounts,
# and deletes data/sandbox/tmp/<job_id>/ for each job, and source, inputs, and
# artifacts all travel through the tool layer in memory — a second temporary
# tree would only add a second cleanup path with nothing to clean.
SANDBOX_TOOL_ENABLED = True

# Where a successful turn's artifacts are published for the user. Unlike the
# per-job scratch space, these survive the turn (that is the point: the user has
# to be able to open the file the agent made) but are still isolated per
# run and per turn, and a failed turn's directory is removed.
SANDBOX_ARTIFACT_ROOT = PROJECT_ROOT / "data" / "artifacts"

# Host margin on the whole-turn deadline check performed before a container is
# started. A job may only begin when the remaining turn time still covers
# execution + cleanup + this margin, so the outer caller-side deadline can never
# abandon a live container before SPEC-015 has killed and removed it.
SANDBOX_TURN_TIME_MARGIN_SECONDS = 2

# Local MCP servers launched by the host over stdio (SPEC-009). Each entry is a
# child process the harness starts; the command, arguments, and environment are
# controlled here by the developer and can never be supplied by the model or by
# chat input. `sys.executable` runs the child in the same virtual environment as
# this app, and the script path is resolved from PROJECT_ROOT so it holds
# regardless of the current working directory.
MCP_SERVERS = {
    "time": {
        "command": sys.executable,
        "args": [str(PROJECT_ROOT / "mcp_servers" / "time_server.py")],
    },
}