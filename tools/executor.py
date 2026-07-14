"""Binds registered tool names to executable handlers and dispatches calls.

`ToolRegistry` (SPEC-006) owns tool *metadata*; a handler owns the *work*. The
`ToolExecutor` is the small seam between them: it knows nothing about the CLI,
Ollama, or persistence — it only looks up a handler by exact tool name and runs
it. A tool may be described in the registry yet have no handler; executing it
must then fail loudly rather than silently do nothing.
"""

from collections.abc import Callable
from typing import Any

from tools.registry import ToolRegistry


ToolArguments = dict[str, Any]
ToolResult = dict[str, Any]
ToolHandler = Callable[[ToolArguments], ToolResult]


class ToolExecutionError(Exception):
    """A protocol/programming error the model cannot resolve by itself.

    Raised for an unknown handler or a handler that violates its contract (e.g.
    returns a non-dict). The caller turns this into an application error and rolls
    back the turn; it is distinct from a tool *failure envelope*, which is valid
    data the model is expected to read and explain.
    """


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._handlers: dict[str, ToolHandler] = {}

    def register_handler(self, name: str, handler: ToolHandler) -> None:
        """Bind a handler to a tool name that already exists in the registry."""

        if name not in self._registry:
            raise ValueError(f"Cannot bind a handler to unknown tool: {name}")
        if name in self._handlers:
            raise ValueError(f"Tool '{name}' already has a handler.")
        self._handlers[name] = handler

    def execute(self, name: str, arguments: ToolArguments) -> ToolResult:
        """Dispatch to the handler bound to `name` and return its envelope."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ToolExecutionError(f"No handler registered for tool: {name}")

        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "error": {
                    "type": "invalid_arguments",
                    "message": "Arguments must be an object.",
                },
            }

        result = handler(arguments)
        if not isinstance(result, dict):
            raise ToolExecutionError(
                f"Handler for '{name}' did not return a result object."
            )
        return result
