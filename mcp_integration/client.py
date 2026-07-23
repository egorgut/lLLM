"""A synchronous manager for the asynchronous MCP client SDK.

The MCP Python SDK is asynchronous, but the rest of lLLM — the CLI loop, the tool
executor, and its handlers — is synchronous. Rather than scatter
``asyncio.run(...)`` across the app or open a fresh process per call, this manager
owns **one** background thread running **one** event loop. Each configured server
is launched once over stdio; its ``ClientSession`` stays open for the lifetime of
the chat inside a single long-lived task on that loop. Tool calls are submitted
to the loop with ``run_coroutine_threadsafe`` and awaited synchronously, so
callers never see a coroutine.

Discovery is fail-fast: if any server cannot be launched, initialized, or queried
with ``tools/list``, ``start()`` raises ``McpStartupError`` before the chat loop
begins and tears down any child it managed to start. ``close()`` is idempotent and
safe to call from a ``finally`` block; after it returns, no child process remains.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_integration.adapter import (
    McpAdapterError,
    namespace_name,
    normalize_result,
    to_tool_spec,
)
from tools import ToolSpec

# Bounds so a wedged child can never hang the CLI indefinitely.
_STARTUP_TIMEOUT = 30.0
_CALL_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 10.0


class McpStartupError(Exception):
    """A startup failure that must abort the application before the chat loop.

    Carries a stable ``error_type`` (from the SPEC-009 startup taxonomy) and the
    ``server_id`` that failed, so the CLI can report it without leaking internals.
    """

    def __init__(self, error_type: str, server_id: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.server_id = server_id


class McpClientManager:
    def __init__(self, servers_config: dict[str, dict[str, Any]]) -> None:
        self._servers_config = servers_config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

        # All of the following are only mutated on the loop thread.
        self._sessions: dict[str, ClientSession] = {}
        self._shutdowns: dict[str, asyncio.Event] = {}
        self._serve_tasks: list[asyncio.Task[Any]] = []

        # Built on the main thread during start() from discovery results.
        self._route_map: dict[str, tuple[str, str]] = {}
        self._specs: list[ToolSpec] = []
        self._counts: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch every configured server, discover its tools, and register them.

        Fail-fast: raises ``McpStartupError`` (after cleaning up) on any failure.
        """

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-loop", daemon=True
        )
        self._thread.start()

        for server_id, cfg in self._servers_config.items():
            params = StdioServerParameters(
                command=cfg["command"],
                args=list(cfg.get("args", [])),
                env=cfg.get("env"),
            )
            ready: ThreadFuture[list[Any]] = ThreadFuture()
            asyncio.run_coroutine_threadsafe(
                self._serve(server_id, params, ready), self._loop
            )
            try:
                tools = ready.result(timeout=_STARTUP_TIMEOUT)
            except McpStartupError:
                self.close()
                raise
            except FutureTimeoutError:
                self.close()
                raise McpStartupError(
                    "mcp_server_start_failed",
                    server_id,
                    "Timed out launching or initializing the MCP server.",
                )
            except Exception:
                self.close()
                raise McpStartupError(
                    "mcp_server_start_failed",
                    server_id,
                    "The MCP server could not be started.",
                )

            self._register_discovered(server_id, tools)

    def _register_discovered(self, server_id: str, tools: list[Any]) -> None:
        count = 0
        for tool in tools:
            try:
                model_facing = namespace_name(server_id, tool.name)
                spec = to_tool_spec(model_facing, tool)
            except McpAdapterError as exc:
                raise McpStartupError(exc.error_type, server_id, str(exc)) from exc

            if model_facing in self._route_map:
                raise McpStartupError(
                    "mcp_tool_name_collision",
                    server_id,
                    f"Duplicate MCP tool name: {model_facing}.",
                )
            self._route_map[model_facing] = (server_id, tool.name)
            self._specs.append(spec)
            count += 1
        self._counts[server_id] = count

    def _run_loop(self) -> None:
        # Hold a local reference: close() clears self._loop from the main thread,
        # so the finally block below must not read the attribute.
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _serve(
        self, server_id: str, params: StdioServerParameters, ready: ThreadFuture
    ) -> None:
        """Own one server's session for its whole lifetime, on the loop thread.

        Enters the stdio transport and client session, initializes, discovers
        tools, hands the tool list back to the waiting main thread, then blocks on
        a shutdown event. Both context managers are entered and exited in this one
        task, so leaving the ``async with`` closes the session and reaps the child.
        """

        self._serve_tasks.append(asyncio.current_task())  # type: ignore[arg-type]
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    try:
                        await session.initialize()
                    except Exception as exc:
                        raise McpStartupError(
                            "mcp_initialize_failed",
                            server_id,
                            "The MCP session could not be initialized.",
                        ) from exc
                    try:
                        listed = await session.list_tools()
                    except Exception as exc:
                        raise McpStartupError(
                            "mcp_tool_discovery_failed",
                            server_id,
                            "The MCP server's tool list could not be read.",
                        ) from exc

                    shutdown = asyncio.Event()
                    self._sessions[server_id] = session
                    self._shutdowns[server_id] = shutdown
                    if not ready.done():
                        ready.set_result(list(listed.tools))
                    await shutdown.wait()
        except McpStartupError as exc:
            if not ready.done():
                ready.set_exception(exc)
        except Exception as exc:
            # A failure before the session opened (e.g. the child could not be
            # spawned) surfaces here as a generic start failure.
            if not ready.done():
                ready.set_exception(
                    McpStartupError(
                        "mcp_server_start_failed",
                        server_id,
                        "The MCP server could not be started.",
                    )
                )
            else:
                print(f"[mcp] server '{server_id}' session ended: {exc}", file=sys.stderr)
        finally:
            self._sessions.pop(server_id, None)

    def close(self) -> None:
        """Close every session and child process, then stop the loop. Idempotent."""

        loop = self._loop
        if loop is None:
            return
        self._loop = None  # further call_tool() invocations now report closed.

        if not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown_all(), loop).result(
                    timeout=_SHUTDOWN_TIMEOUT
                )
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT + 1.0)
            self._thread = None

    async def _shutdown_all(self) -> None:
        for event in self._shutdowns.values():
            event.set()
        pending = [task for task in self._serve_tasks if not task.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=_SHUTDOWN_TIMEOUT)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.wait(still_pending, timeout=_SHUTDOWN_TIMEOUT)

    # -- discovery results ---------------------------------------------------

    def tool_specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def server_summaries(self) -> list[str]:
        summaries = []
        for server_id, count in self._counts.items():
            noun = "tool" if count == 1 else "tools"
            summaries.append(f"{server_id} ({count} {noun})")
        return summaries

    # -- execution -----------------------------------------------------------

    def call_tool(self, model_facing_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a discovered MCP tool by its model-facing name (synchronous).

        Returns the normalized JSON-compatible envelope. Transport, session, and
        normalization failures are mapped to stable error envelopes — no
        tracebacks, paths, environment, or raw stderr reach the caller.
        """

        route = self._route_map.get(model_facing_name)
        if route is None:
            return _error_envelope(
                None, model_facing_name, "mcp_call_failed", "Unknown MCP tool."
            )
        server_id, remote_name = route

        loop = self._loop
        if loop is None or loop.is_closed():
            return _error_envelope(
                server_id, remote_name, "mcp_server_closed", "The MCP session is closed."
            )

        try:
            return asyncio.run_coroutine_threadsafe(
                self._invoke(server_id, remote_name, arguments), loop
            ).result(timeout=_CALL_TIMEOUT)
        except Exception:
            return _error_envelope(
                server_id, remote_name, "mcp_call_failed", "The MCP tool call failed."
            )

    async def _invoke(
        self, server_id: str, remote_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._sessions.get(server_id)
        if session is None:
            return _error_envelope(
                server_id, remote_name, "mcp_server_closed", "The MCP session is closed."
            )
        try:
            result = await session.call_tool(remote_name, arguments)
        except Exception:
            return _error_envelope(
                server_id, remote_name, "mcp_call_failed", "The MCP tool call failed."
            )
        try:
            return normalize_result(server_id, remote_name, result)
        except Exception:
            return _error_envelope(
                server_id,
                remote_name,
                "mcp_invalid_result",
                "The MCP tool returned an unreadable result.",
            )


def _error_envelope(
    server_id: str | None, tool: str, error_type: str, message: str
) -> dict[str, Any]:
    return {
        "ok": False,
        "server": server_id,
        "tool": tool,
        "error": {"type": error_type, "message": message},
    }
