import argparse
import json
import os
import time
from pathlib import Path

from cli_activity import ActivityIndicator
from config import (
    CHAT_HISTORY_PATH,
    MAX_IDENTICAL_TOOL_CALLS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_INSTRUCTION_CHARS,
    MAX_SKILL_ROUTING_RESPONSE_CHARS,
    MAX_SKILL_SCHEMA_BYTES,
    MAX_SKILLS,
    MAX_TOOL_CALLS_PER_TURN,
    MCP_SERVERS,
    MODEL_PROFILES,
    PROJECT_ROOT,
    SANDBOX_ARTIFACT_ROOT,
    SANDBOX_TOOL_ENABLED,
    SANDBOX_TURN_TIME_MARGIN_SECONDS,
    SKILL_ROUTING_REPAIR_ATTEMPTS,
    SKILLS_ROOT,
    SQLITE_DATABASE_PATH,
    TOOL_EXECUTION_TIMEOUT_SECONDS,
    TRACE_DIR,
    TRACE_ENABLED,
    TRACE_PAYLOAD_PREVIEW_CHARS,
    TRACKER_MCP_SERVER_ID,
    ModelProfile,
    resolve_model_profile,
)
from conversation import Conversation
from llm import ModelToolCall, OllamaModel
from mcp_integration import (
    McpClientManager,
    McpServerConfig,
    McpStartupError,
    load_tracker_server_config,
)
from reliability import TurnStatus, new_id, validate_reliability_config
from sandbox_tool import (
    SKILL_NAME as SANDBOX_SKILL_NAME,
    SandboxCapability,
    build_sandbox_capability,
)
from skill_runtime import (
    SkillPackageError,
    SkillPackageLoader,
    SkillRouter,
    SkillTurnOrchestrator,
    validate_skill_config,
)
from skill_runtime.models import SkillSelection
from storage import JsonConversationStore
from tools import (
    PYTHON_CALCULATE_SPEC,
    SQL_QUERY_SPEC,
    ToolExecutor,
    ToolRegistry,
    create_sql_query_handler,
    python_calculate,
)
from tracing import (
    JsonlTraceSink,
    NullTraceSink,
    SafeTraceSink,
    build_event,
    trace_file_path,
)

_DOTENV_PATH = PROJECT_ROOT / ".env"


def load_dotenv_if_present(path: Path = _DOTENV_PATH) -> None:
    """Fill in missing process environment variables from a local `.env` file.

    A tiny, dependency-free stand-in for `python-dotenv`, in keeping with this
    project's framework-free ethos (SPEC-013 explicitly allows this "unless
    implementation explicitly chooses and documents it" -- this is that
    choice). A variable already present in the real process environment
    always wins: this only fills gaps, exactly as if you had exported it
    yourself. A missing `.env` is not an error -- the file is optional, and a
    fresh checkout never needs one (SPEC-013 Tracker integration stays
    disabled by default either way).

    Supports only `KEY=value` lines, optional single/double-quoted values,
    blank lines, and full-line `#` comments -- no interpolation, no export
    keyword, no multi-line values.
    """

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def build_executor() -> tuple[ToolRegistry, ToolExecutor]:
    registry = ToolRegistry()
    registry.register(PYTHON_CALCULATE_SPEC)
    registry.register(SQL_QUERY_SPEC)
    executor = ToolExecutor(registry)
    executor.register_handler("python_calculate", python_calculate)
    executor.register_handler("sql_query", create_sql_query_handler(SQLITE_DATABASE_PATH))
    return registry, executor


def build_mcp_servers() -> dict[str, McpServerConfig]:
    """The effective MCP server map: static local servers + conditional Tracker.

    Reused identically by the live evaluation suite (``evals/runner.py``) so
    both entry points construct exactly the same ``McpClientManager`` input.
    """

    servers: dict[str, McpServerConfig] = {}
    for server_id, cfg in MCP_SERVERS.items():
        servers[server_id] = McpServerConfig(
            server_id=server_id,
            command=cfg["command"],
            args=tuple(cfg.get("args", [])),
            env=dict(cfg.get("env") or {}),
            enabled=cfg.get("enabled", True),
            allowed_tools=cfg.get("allowed_tools"),
            required_tools=frozenset(cfg.get("required_tools", ())),
        )
    servers[TRACKER_MCP_SERVER_ID] = load_tracker_server_config()
    return servers


def register_mcp_tools(
    registry: ToolRegistry,
    executor: ToolExecutor,
    manager: McpClientManager,
    mcp_servers: dict[str, McpServerConfig],
) -> None:
    """Register every discovered MCP tool beside the local tools.

    Each converted ToolSpec joins the shared registry, and a small synchronous
    adapter handler routes the model-selected call back through the MCP manager.
    A registration conflict is a startup error and aborts before the chat loop.
    Only tools already admitted by ``manager`` (i.e. that survived host-owned
    allowlist filtering) ever reach this point.
    """

    for spec in manager.tool_specs():
        try:
            registry.register(spec)
        except (ValueError, TypeError) as error:
            raise McpStartupError(
                "mcp_tool_name_collision",
                _server_for(spec.name, mcp_servers),
                f"Could not register MCP tool '{spec.name}': {error}",
            ) from error
        executor.register_handler(
            spec.name,
            lambda arguments, name=spec.name: manager.call_tool(name, arguments),
        )


def _server_for(model_facing_name: str, mcp_servers: dict[str, McpServerConfig]) -> str:
    for server_id in mcp_servers:
        if model_facing_name.startswith(f"mcp_{server_id}__"):
            return server_id
    return "?"


class CliRenderer:
    """Renders agent-loop output to the terminal.

    Holds the one bit of per-turn state the loop needs: whether the lazy
    ``Qwen: `` prefix has been printed yet, so it appears exactly once, only when
    real final-answer text arrives. A turn that resolves entirely through tool
    calls never shows an empty ``Qwen:`` line.

    It also drives the CLI's activity indicator (PATCH-010-01), because the
    renderer is what knows when a silent stretch ends and when the next one
    begins. The indicator itself is owned by the chat loop, since the first
    silence -- skill routing -- starts before any renderer exists.
    """

    def __init__(self, indicator: ActivityIndicator | None = None) -> None:
        self._printed_prefix = False
        self._indicator = (
            indicator if indicator is not None else ActivityIndicator(enabled=False)
        )

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None:
        self._indicator.stop()
        print(f"\n[tool {used}/{maximum}] {call.name}")
        print(f"[args] {json.dumps(call.arguments, ensure_ascii=False)}")
        # Executing the tool is silent too, and a sandbox call can take a while.
        self._indicator.start()

    def tool_result(self, result: dict) -> None:
        self._indicator.stop()
        print(f"[result] {json.dumps(result, ensure_ascii=False)}")
        # The model's next decision follows, with nothing to show until it lands.
        self._indicator.start()

    def text(self, chunk: str) -> None:
        if not self._printed_prefix:
            # The final answer has begun: stop for good, once, rather than on
            # every chunk. Runs on the loop's worker thread (agent.py).
            self._indicator.stop()
            print("\nQwen: ", end="", flush=True)
            self._printed_prefix = True
        print(chunk, end="", flush=True)


def _rollback_artifacts(sandbox: SandboxCapability | None) -> None:
    """Discard an unsuccessful turn's staged artifacts.

    Never raises: a failed turn is already being reported, and a filesystem
    problem while cleaning up must not replace that message with a traceback.
    """

    if sandbox is None:
        return
    try:
        sandbox.workspace.rollback()
    except Exception:  # noqa: BLE001 - cleanup must not mask the turn's outcome
        pass


def describe_profile(profile: ModelProfile) -> str:
    """The one-line startup summary of what this run is talking to."""

    return (
        f"{profile.name}: {profile.model} "
        f"(request {profile.model_request_timeout_seconds:g}s, "
        f"turn {profile.agent_turn_timeout_seconds:g}s, "
        f"routing {profile.skill_routing_timeout_seconds:g}s)"
    )


def validate_model_profiles() -> None:
    """Check every committed profile, not just the selected one (SPEC-017 §4.3).

    A profile whose deadlines are internally incoherent is a repository defect
    that would otherwise surface only for whoever first selects it, mid-turn.
    The same host-owned rules SPEC-011 §10 applies to a single configuration
    apply to each profile; the tool-execution budget and call limits are global,
    so every profile is checked against those.
    """

    for name, profile in MODEL_PROFILES.items():
        try:
            validate_reliability_config(
                model_request_timeout_seconds=profile.model_request_timeout_seconds,
                tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
                agent_turn_timeout_seconds=profile.agent_turn_timeout_seconds,
                max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
                max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
            )
        except ValueError as error:
            raise ValueError(f"Model profile '{name}' is invalid: {error}") from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local AI chat.")
    parser.add_argument(
        "--profile",
        choices=sorted(MODEL_PROFILES),
        default=None,
        help=(
            "Model profile to run under: which model, and the deadlines that "
            "model needs (SPEC-017). Defaults to the host default profile."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # The profile is resolved before anything else: it decides which model runs
    # and which deadlines bound it, and an unknown name must never reach the
    # chat loop (SPEC-017 §4.3).
    validate_model_profiles()
    profile = resolve_model_profile(args.profile)

    # Fill any gaps in the process environment from a local `.env` before
    # anything (e.g. Tracker's config loader) reads it. Real exported
    # variables always take precedence; a missing file is a no-op.
    load_dotenv_if_present()

    # One run_id identifies this whole process; a fresh turn_id correlates
    # every trace event and CLI diagnostic for one user turn (SPEC-011 §3).
    run_id = new_id()
    sink = (
        JsonlTraceSink(trace_file_path(TRACE_DIR, run_id))
        if TRACE_ENABLED
        else NullTraceSink()
    )
    trace_sink = SafeTraceSink(sink, run_id)
    run_started_at = time.monotonic()
    trace_sink.emit(
        build_event(
            "run_started",
            run_id=run_id,
            model_name=profile.model,
            model_profile=profile.name,
            app_version=None,
        )
    )

    store = JsonConversationStore(CHAT_HISTORY_PATH)
    conversation = Conversation(messages=store.load())
    registry, executor = build_executor()

    # The sandbox is an optional capability (SPEC-016 §11.2): it needs a running
    # Docker daemon holding the pinned image, and neither is something a fresh
    # checkout has. When either is missing the tool is never registered and the
    # skill is omitted, so the model is never offered a capability that could
    # only fail — normal chat and every other tool are unaffected. The
    # diagnostic is printed later, beside the MCP and skill lines.
    sandbox, sandbox_diagnostic = build_sandbox_capability(
        run_id=run_id,
        artifact_root=SANDBOX_ARTIFACT_ROOT,
        project_root=PROJECT_ROOT,
        turn_time_margin_seconds=SANDBOX_TURN_TIME_MARGIN_SECONDS,
        enabled=SANDBOX_TOOL_ENABLED,
        trace_sink=trace_sink,
    )
    if sandbox is not None:
        registry.register(sandbox.spec)
        executor.register_handler(sandbox.spec.name, sandbox.handler)

    # MCP tool discovery is fail-fast and happens before the chat loop. If a
    # server cannot be launched, initialized, or queried, report it clearly (no
    # traceback) and exit without leaving a child process behind. The manager's
    # own per-call timeout is host-owned, matching the tool-execution deadline
    # AgentRunner enforces around every call (mcp_integration/client.py).
    # `manager` starts as None: building the effective server map itself can
    # fail-fast (e.g. Tracker enabled but misconfigured), before any child
    # process exists, so the closing `finally` below must tolerate that.
    manager: McpClientManager | None = None

    try:
        try:
            mcp_servers = build_mcp_servers()
            manager = McpClientManager(
                mcp_servers,
                call_timeout=TOOL_EXECUTION_TIMEOUT_SECONDS,
                run_id=run_id,
                trace_sink=trace_sink,
            )
            manager.start()
            register_mcp_tools(registry, executor, manager, mcp_servers)
        except McpStartupError as error:
            print(f"MCP startup failed for server '{error.server_id}': {error}")
            raise SystemExit(1)

        tools = registry.to_ollama_tools()

        # Skills are validated against the FINAL tool registry (local + MCP), so
        # this must run after MCP registration. A malformed package or a reference
        # to an unavailable tool is a fail-fast startup error, not a turn-time
        # event (SPEC-012 §15). The surrounding `finally` still closes MCP.
        validate_skill_config(
            skill_routing_timeout_seconds=profile.skill_routing_timeout_seconds,
            skill_routing_repair_attempts=SKILL_ROUTING_REPAIR_ATTEMPTS,
            max_skill_routing_response_chars=MAX_SKILL_ROUTING_RESPONSE_CHARS,
            max_skill_instruction_chars=MAX_SKILL_INSTRUCTION_CHARS,
            max_skill_schema_bytes=MAX_SKILL_SCHEMA_BYTES,
            max_skills=MAX_SKILLS,
            max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
        )
        # tracker_read depends entirely on the (possibly disabled) Tracker
        # integration; when it is disabled, its tools were never registered,
        # so the package is omitted deterministically as one configured
        # feature unit instead of failing the whole skill-loading pass
        # (SPEC-013 §"Tracker integration is disabled").
        omit_skills = (
            frozenset() if mcp_servers[TRACKER_MCP_SERVER_ID].enabled else frozenset({"tracker_read"})
        )
        # code_workspace's only tool is sandbox_execute, so an unavailable or
        # disabled sandbox omits the package by the same rule (SPEC-016 §19.4).
        if sandbox is None:
            omit_skills = omit_skills | frozenset({SANDBOX_SKILL_NAME})
        try:
            skill_registry = SkillPackageLoader().load_all(SKILLS_ROOT, registry, omit=omit_skills)
        except SkillPackageError as error:
            print(f"Application startup failed: {error}")
            raise SystemExit(1)

        # The transport is built here from the profile and injected, rather than
        # read from a module-level constant at import time (SPEC-017 §4.4). The
        # router and the agent loop share it, so both always speak to the same
        # model under the same client timeout.
        model = OllamaModel.for_profile(profile)

        router = SkillRouter(
            route=model.text,
            timeout_seconds=profile.skill_routing_timeout_seconds,
            max_response_chars=MAX_SKILL_ROUTING_RESPONSE_CHARS,
            repair_attempts=SKILL_ROUTING_REPAIR_ATTEMPTS,
            payload_preview_chars=TRACE_PAYLOAD_PREVIEW_CHARS,
        )

        # One indicator for the whole session, started and stopped per turn. It
        # is created here rather than inside CliRenderer because the turn's first
        # silence is skill routing, which happens before a renderer exists
        # (PATCH-010-01). Outside an interactive terminal it is a no-op.
        indicator = ActivityIndicator()

        def announce_skill(selection: SkillSelection) -> None:
            # Print the [skill] line only when a skill is selected, before the
            # agent loop's tool/answer output (SPEC-012 §"User-visible behavior").
            # This line is printed outside the renderer, so it clears the
            # indicator itself; the agent loop's own silence follows right after.
            if selection.skill_name:
                indicator.stop()
                print(f"[skill] {selection.skill_name}")
                indicator.start()

        orchestrator = SkillTurnOrchestrator(
            skill_registry=skill_registry,
            router=router,
            tool_registry=registry,
            executor=executor,
            respond=model.respond,
            renderer_factory=lambda: CliRenderer(indicator),
            default_tools=tools,
            run_id=run_id,
            max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
            max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
            model_request_timeout_seconds=profile.model_request_timeout_seconds,
            tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
            agent_turn_timeout_seconds=profile.agent_turn_timeout_seconds,
            trace_sink=trace_sink,
            payload_preview_chars=TRACE_PAYLOAD_PREVIEW_CHARS,
            on_selection=announce_skill,
            # Binds each sandbox turn to the turn_id and deadline the
            # orchestrator mints, before routing spends any of it.
            on_turn_context=(
                sandbox.workspace.begin_turn if sandbox else lambda _context: None
            ),
            # A sandbox call's arguments are the user's data as code and files,
            # not parameters worth previewing into a local trace (SPEC-016 §15.3).
            redacted_argument_tools=(
                frozenset({sandbox.spec.name}) if sandbox else frozenset()
            ),
        )

        print(f"[model] {describe_profile(profile)}")
        for summary in manager.server_summaries():
            print(f"[mcp] {summary}")
        print(sandbox_diagnostic)
        if sandbox is None:
            print(f"[skills] {SANDBOX_SKILL_NAME}: omitted")
        if len(skill_registry):
            names = ", ".join(entry.name for entry in skill_registry.catalog())
            print(f"[skills] {len(skill_registry)} loaded: {names}")

        print("Local AI chat")
        print("Enter /reset to clear the conversation, /bye to exit.\n")

        while True:
            # Ctrl+D (EOF) or Ctrl+C at the prompt ends the session cleanly and
            # falls through to the guaranteed MCP shutdown below.
            try:
                user_message = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_message:
                continue

            if user_message.lower() == "/bye":
                print("Chat finished.")
                break

            if user_message.lower() == "/reset":
                conversation.reset()
                store.save(conversation.stored_messages)
                print("Conversation cleared.\n")
                continue

            # Tentatively append the user message. The orchestrator routes to
            # zero or one skill and drives the bounded model->tool->model loop
            # over a snapshot of the model-facing messages; routing and execution
            # share one turn_id and one whole-turn deadline. Routing protocol
            # messages are never added to the conversation.
            conversation.add_user_message(user_message)

            try:
                # The indicator covers every silent stretch of the turn and is
                # always cleared on the way out -- controlled error, timeout,
                # KeyboardInterrupt, or an unexpected exception alike -- so the
                # outcome branches below always print on a clean line.
                with indicator:
                    result = orchestrator.run_turn(conversation)
            except Exception:
                # A safety net beyond AgentRunner's own internal_error
                # conversion: the terminal trace event is already guaranteed by
                # run_turn itself before this exception reaches us.
                print(
                    "\nApplication error: Unexpected application error.\n"
                    f"Run ID: {run_id}\n"
                )
                _rollback_artifacts(sandbox)
                conversation.remove_last_message()
                continue

            outcome = result.outcome
            # Only a completed turn persists; every other outcome (including any
            # routing failure) rolls back the tentative user message. Sandbox
            # artifacts follow exactly the same rule (SPEC-016 §9.5): a turn the
            # user never got an answer from leaves no files behind either.
            if outcome.status is TurnStatus.COMPLETED:
                if sandbox is not None:
                    sandbox.workspace.commit()
                conversation.add_assistant_message(outcome.final_text)
                store.save(conversation.stored_messages)
            elif outcome.status is TurnStatus.CANCELLED:
                print(f"\n{outcome.error_message}\nRun ID: {outcome.turn_id}\n")
                _rollback_artifacts(sandbox)
                conversation.remove_last_message()
            else:
                print(
                    f"\nApplication error: {outcome.error_message}\n"
                    f"Run ID: {outcome.turn_id}\n"
                )
                _rollback_artifacts(sandbox)
                conversation.remove_last_message()

            print("\n")
    finally:
        # Runs on /bye, EOF, Ctrl+C, normal completion, MCP startup failure, and
        # any escaping exception — the MCP session and child process are always
        # closed, and the run is always closed out in the trace. `manager` may
        # still be None if building the effective server map itself failed
        # (e.g. Tracker enabled but misconfigured) before any child existed.
        if manager is not None:
            manager.close()
        trace_sink.emit(
            build_event(
                "run_finished",
                run_id=run_id,
                duration_ms=int((time.monotonic() - run_started_at) * 1000),
            )
        )


if __name__ == "__main__":
    main()
