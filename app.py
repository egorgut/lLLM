import json
import time

from agent import AgentRunner
from config import (
    AGENT_TURN_TIMEOUT_SECONDS,
    CHAT_HISTORY_PATH,
    MAX_IDENTICAL_TOOL_CALLS,
    MAX_TOOL_CALLS_PER_TURN,
    MCP_SERVERS,
    MODEL_NAME,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    SQLITE_DATABASE_PATH,
    TOOL_EXECUTION_TIMEOUT_SECONDS,
    TRACE_ENABLED,
    TRACE_PATH,
)
from conversation import Conversation
from llm import ModelResponse, ModelToolCall
from mcp_integration import McpClientManager, McpStartupError
from reliability import TurnStatus, new_id
from storage import JsonConversationStore
from tools import (
    PYTHON_CALCULATE_SPEC,
    SQL_QUERY_SPEC,
    ToolExecutor,
    ToolRegistry,
    create_sql_query_handler,
    python_calculate,
)
from tracing import JsonlTraceSink, NullTraceSink, SafeTraceSink, build_event


def build_executor() -> tuple[ToolRegistry, ToolExecutor]:
    registry = ToolRegistry()
    registry.register(PYTHON_CALCULATE_SPEC)
    registry.register(SQL_QUERY_SPEC)
    executor = ToolExecutor(registry)
    executor.register_handler("python_calculate", python_calculate)
    executor.register_handler("sql_query", create_sql_query_handler(SQLITE_DATABASE_PATH))
    return registry, executor


def register_mcp_tools(
    registry: ToolRegistry, executor: ToolExecutor, manager: McpClientManager
) -> None:
    """Register every discovered MCP tool beside the local tools.

    Each converted ToolSpec joins the shared registry, and a small synchronous
    adapter handler routes the model-selected call back through the MCP manager.
    A registration conflict is a startup error and aborts before the chat loop.
    """

    for spec in manager.tool_specs():
        try:
            registry.register(spec)
        except (ValueError, TypeError) as error:
            raise McpStartupError(
                "mcp_tool_name_collision",
                _server_for(spec.name),
                f"Could not register MCP tool '{spec.name}': {error}",
            ) from error
        executor.register_handler(
            spec.name,
            lambda arguments, name=spec.name: manager.call_tool(name, arguments),
        )


def _server_for(model_facing_name: str) -> str:
    for server_id in MCP_SERVERS:
        if model_facing_name.startswith(f"mcp_{server_id}__"):
            return server_id
    return "?"


class CliRenderer:
    """Renders agent-loop output to the terminal.

    Holds the one bit of per-turn state the loop needs: whether the lazy
    ``Qwen: `` prefix has been printed yet, so it appears exactly once, only when
    real final-answer text arrives. A turn that resolves entirely through tool
    calls never shows an empty ``Qwen:`` line.
    """

    def __init__(self) -> None:
        self._printed_prefix = False

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None:
        print(f"\n[tool {used}/{maximum}] {call.name}")
        print(f"[args] {json.dumps(call.arguments, ensure_ascii=False)}")

    def tool_result(self, result: dict) -> None:
        print(f"[result] {json.dumps(result, ensure_ascii=False)}")

    def text(self, chunk: str) -> None:
        if not self._printed_prefix:
            print("\nQwen: ", end="", flush=True)
            self._printed_prefix = True
        print(chunk, end="", flush=True)


def main() -> None:
    # One run_id identifies this whole process; a fresh turn_id correlates
    # every trace event and CLI diagnostic for one user turn (SPEC-011 §3).
    run_id = new_id()
    sink = JsonlTraceSink(TRACE_PATH) if TRACE_ENABLED else NullTraceSink()
    trace_sink = SafeTraceSink(sink, run_id)
    run_started_at = time.monotonic()
    trace_sink.emit(
        build_event("run_started", run_id=run_id, model_name=MODEL_NAME, app_version=None)
    )

    store = JsonConversationStore(CHAT_HISTORY_PATH)
    conversation = Conversation(messages=store.load())
    registry, executor = build_executor()

    # MCP tool discovery is fail-fast and happens before the chat loop. If a
    # server cannot be launched, initialized, or queried, report it clearly (no
    # traceback) and exit without leaving a child process behind. The manager's
    # own per-call timeout is host-owned, matching the tool-execution deadline
    # AgentRunner enforces around every call (mcp_integration/client.py).
    manager = McpClientManager(MCP_SERVERS, call_timeout=TOOL_EXECUTION_TIMEOUT_SECONDS)

    try:
        try:
            manager.start()
            register_mcp_tools(registry, executor, manager)
        except McpStartupError as error:
            print(f"MCP startup failed for server '{error.server_id}': {error}")
            raise SystemExit(1)

        tools = registry.to_ollama_tools()
        for summary in manager.server_summaries():
            print(f"[mcp] connected: {summary}")

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

            conversation.add_user_message(user_message)
            turn_id = new_id()

            # A fresh renderer per turn resets the lazy 'Qwen:' prefix state. The
            # runner drives the bounded model->tool->model loop over a snapshot of
            # the model-facing messages; it never sees the mutable Conversation.
            runner = AgentRunner(
                respond=lambda messages, declarations: ModelResponse(
                    messages, declarations
                ),
                executor=executor,
                tools=tools,
                renderer=CliRenderer(),
                run_id=run_id,
                max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
                max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
                model_request_timeout_seconds=MODEL_REQUEST_TIMEOUT_SECONDS,
                tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
                agent_turn_timeout_seconds=AGENT_TURN_TIMEOUT_SECONDS,
                trace_sink=trace_sink,
            )

            try:
                outcome = runner.run_turn(conversation.messages_for_model, turn_id=turn_id)
            except Exception:
                # A safety net beyond AgentRunner's own internal_error
                # conversion: the terminal trace event is already guaranteed by
                # run_turn itself before this exception reaches us.
                print(
                    "\nApplication error: Unexpected application error.\n"
                    f"Run ID: {turn_id}\n"
                )
                conversation.remove_last_message()
                continue

            if outcome.status is TurnStatus.COMPLETED:
                conversation.add_assistant_message(outcome.final_text)
                store.save(conversation.stored_messages)
            elif outcome.status is TurnStatus.CANCELLED:
                print(f"\n{outcome.error_message}\nRun ID: {turn_id}\n")
                conversation.remove_last_message()
            else:
                print(f"\nApplication error: {outcome.error_message}\nRun ID: {turn_id}\n")
                conversation.remove_last_message()

            print("\n")
    finally:
        # Runs on /bye, EOF, Ctrl+C, normal completion, MCP startup failure, and
        # any escaping exception — the MCP session and child process are always
        # closed, and the run is always closed out in the trace.
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
