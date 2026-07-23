import json

from agent import AgentRunner
from config import (
    CHAT_HISTORY_PATH,
    MAX_TOOL_CALLS_PER_TURN,
    MCP_SERVERS,
    SQLITE_DATABASE_PATH,
)
from conversation import Conversation
from llm import ModelResponse, ModelToolCall
from mcp_integration import McpClientManager, McpStartupError
from storage import JsonConversationStore
from tools import (
    PYTHON_CALCULATE_SPEC,
    SQL_QUERY_SPEC,
    ToolExecutor,
    ToolRegistry,
    create_sql_query_handler,
    python_calculate,
)


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
    store = JsonConversationStore(CHAT_HISTORY_PATH)
    conversation = Conversation(messages=store.load())
    registry, executor = build_executor()

    # MCP tool discovery is fail-fast and happens before the chat loop. If a
    # server cannot be launched, initialized, or queried, report it clearly (no
    # traceback) and exit without leaving a child process behind.
    manager = McpClientManager(MCP_SERVERS)
    try:
        manager.start()
        register_mcp_tools(registry, executor, manager)
    except McpStartupError as error:
        manager.close()
        print(f"MCP startup failed for server '{error.server_id}': {error}")
        raise SystemExit(1)

    tools = registry.to_ollama_tools()
    for summary in manager.server_summaries():
        print(f"[mcp] connected: {summary}")

    print("Local AI chat")
    print("Enter /reset to clear the conversation, /bye to exit.\n")

    try:
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

            # A fresh renderer per turn resets the lazy 'Qwen:' prefix state. The
            # runner drives the bounded model->tool->model loop over a snapshot of
            # the model-facing messages; it never sees the mutable Conversation.
            runner = AgentRunner(
                respond=lambda messages, declarations: ModelResponse(
                    messages, declarations
                ),
                executor=executor,
                tools=tools,
                max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
                renderer=CliRenderer(),
            )

            try:
                assistant_message = runner.run_turn(conversation.messages_for_model)
            except KeyboardInterrupt:
                print("\nGeneration interrupted.\n")

                # Turn did not complete — roll back the user message.
                conversation.remove_last_message()
                continue
            except Exception as error:
                print(f"\nApplication error: {error}\n")

                # No complete answer was produced — roll back the user message.
                conversation.remove_last_message()
                continue

            conversation.add_assistant_message(assistant_message)
            store.save(conversation.stored_messages)

            print("\n")
    finally:
        # Runs on /bye, EOF, Ctrl+C, normal completion, and any escaping
        # exception — the MCP session and child process are always closed.
        manager.close()


if __name__ == "__main__":
    main()
