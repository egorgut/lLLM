import json

from config import CHAT_HISTORY_PATH, MCP_SERVERS, SQLITE_DATABASE_PATH
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


class TurnError(Exception):
    """A controlled failure that aborts the current turn with a clear message."""


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


def render_tool_call(call: ModelToolCall) -> None:
    print(f"\n[tool] {call.name}")
    print(f"[args] {json.dumps(call.arguments, ensure_ascii=False)}")


def render_tool_result(result: dict) -> None:
    print(f"[result] {json.dumps(result, ensure_ascii=False)}")


def stream_response(response: ModelResponse, parts: list[str]) -> None:
    """Stream text to the CLI, printing the 'Qwen: ' prefix lazily.

    The prefix appears only once real text arrives, so a turn that resolves to a
    tool call (no text) never shows an empty 'Qwen:' line.
    """

    printed_prefix = False
    for chunk in response.text_chunks():
        if not printed_prefix:
            print("\nQwen: ", end="", flush=True)
            printed_prefix = True
        print(chunk, end="", flush=True)
        parts.append(chunk)


def assistant_tool_message(call: ModelToolCall) -> dict:
    """The temporary assistant message that records the tool call for the model."""

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
    }


def tool_result_message(call: ModelToolCall, result: dict) -> dict:
    """The temporary tool-result message sent back to the model."""

    return {
        "role": "tool",
        "tool_name": call.name,
        "content": json.dumps(result, ensure_ascii=False),
    }


def run_turn(
    conversation: Conversation, executor: ToolExecutor, tools: list[dict]
) -> str:
    """Run one user turn and return the final assistant answer.

    Supports at most one tool execution. Raises TurnError (or lets other
    exceptions propagate) when a complete final answer cannot be produced; the
    caller is responsible for rolling back the user message.
    """

    first = ModelResponse(conversation.messages_for_model, tools)
    parts: list[str] = []
    stream_response(first, parts)

    # No tool requested: this is a normal streamed text answer.
    if not first.tool_calls:
        message = "".join(parts)
        if not message:
            raise TurnError("Model returned an empty response.")
        return message

    if len(first.tool_calls) > 1:
        raise TurnError("Multiple tool calls are not supported.")

    # One tool call: show it, execute it, and send the result back to the model.
    call = first.tool_calls[0]
    render_tool_call(call)
    result = executor.execute(call.name, call.arguments)
    render_tool_result(result)

    second_messages = [
        *conversation.messages_for_model,
        assistant_tool_message(call),
        tool_result_message(call, result),
    ]
    second = ModelResponse(second_messages, tools)
    final_parts: list[str] = []
    stream_response(second, final_parts)

    if second.tool_calls:
        raise TurnError("Additional tool calls are not supported after a tool result.")

    final_message = "".join(final_parts)
    if not final_message:
        raise TurnError("Model returned an empty response.")
    return final_message


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

            try:
                assistant_message = run_turn(conversation, executor, tools)
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
