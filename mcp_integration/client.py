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
import time
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_integration.adapter import McpAdapterError, normalize_result
from mcp_integration.policy import McpAdmissionError, filter_discovered_tools
from tools import ToolSpec
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event

if TYPE_CHECKING:
    from mcp_integration.config import McpServerConfig

# Bounds so a wedged child can never hang the CLI indefinitely. The call
# timeout is host-configurable (SPEC-011 §10); the other two are internal
# lifecycle bounds not exposed to the rest of the application.
_STARTUP_TIMEOUT = 30.0
_DEFAULT_CALL_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 10.0


def mcp_log_path(directory: str | Path, run_id: str) -> Path:
    """Where this run's MCP children write their own stderr (PATCH-009-01).

    One file per run rather than one growing shared file, for the same reasons
    PATCH-011-01 gave for traces: two concurrent processes never append to the
    same path, and correlating a run means picking a file rather than filtering
    one.
    """

    return Path(directory) / f"mcp-{run_id}.log"


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
    def __init__(
        self,
        servers_config: dict[str, "McpServerConfig"],
        call_timeout: float = _DEFAULT_CALL_TIMEOUT,
        *,
        run_id: str,
        trace_sink: TraceSink = NullTraceSink(),
        log_dir: str | Path | None = None,
    ) -> None:
        self._servers_config = servers_config
        self._call_timeout = call_timeout
        self._run_id = run_id
        self._trace = SafeTraceSink(trace_sink, run_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # Where child stderr goes. `None` keeps the SDK's historical default --
        # this process's stderr -- so the parameter is purely additive and an
        # existing caller that does not pass it behaves exactly as before.
        self._log_dir = log_dir
        self._errlog: TextIO | None = None

        # All of the following are only mutated on the loop thread.
        self._sessions: dict[str, ClientSession] = {}
        self._shutdowns: dict[str, asyncio.Event] = {}
        self._serve_tasks: list[asyncio.Task[Any]] = []

        # Built on the main thread during start() from discovery results.
        self._route_map: dict[str, tuple[str, str]] = {}
        self._specs: list[ToolSpec] = []
        self._counts: dict[str, int] = {}
        self._filtered_counts: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    @property
    def log_path(self) -> Path | None:
        """This run's MCP log file, or ``None`` when children inherit stderr.

        Exposed so the entry point can name it in the otherwise deliberately
        anonymous startup-failure message (SPEC-009 keeps tracebacks, paths, and
        raw stderr out of `McpStartupError` itself). A location is not content.
        """

        if self._log_dir is None:
            return None
        return mcp_log_path(self._log_dir, self._run_id)

    def _open_errlog(self) -> None:
        """Open this run's log file, if one is configured. Never fatal.

        A log destination that cannot be opened must not stop the chat: the
        fallback is the historical behaviour (child output on stderr), which is
        noisy but working, and strictly better than refusing to start over a
        logging concern.
        """

        path = self.log_path
        if path is None or self._errlog is not None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._errlog = path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            self._errlog = None

    def _child_errlog(self) -> TextIO:
        return self._errlog if self._errlog is not None else sys.stderr

    def _close_errlog(self) -> None:
        errlog, self._errlog = self._errlog, None
        if errlog is None:
            return
        try:
            errlog.close()
        except OSError:  # pragma: no cover - closing a local file rarely fails
            pass

    def start(self) -> None:
        """Launch every configured server, discover its tools, and register them.

        Fail-fast: raises ``McpStartupError`` (after cleaning up) on any failure.
        """

        self._open_errlog()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-loop", daemon=True
        )
        self._thread.start()

        for server_id, cfg in self._servers_config.items():
            if not cfg.enabled:
                continue

            self._trace.emit(
                build_event(
                    "mcp_server_starting",
                    run_id=self._run_id,
                    server=server_id,
                    transport="stdio",
                )
            )
            started_at = time.monotonic()
            params = StdioServerParameters(
                command=cfg.command,
                args=list(cfg.args),
                env=dict(cfg.env) if cfg.env else None,
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

            self._register_discovered(server_id, tools, cfg, started_at=started_at)

    def _register_discovered(
        self, server_id: str, tools: list[Any], cfg: "McpServerConfig", *, started_at: float
    ) -> None:
        try:
            result = filter_discovered_tools(
                server_id,
                tools,
                allowed_tools=cfg.allowed_tools,
                required_tools=cfg.required_tools,
            )
        except (McpAdmissionError, McpAdapterError) as exc:
            raise McpStartupError(exc.error_type, server_id, str(exc)) from exc

        if result.missing_required_names:
            raise McpStartupError(
                "mcp_required_tool_missing",
                server_id,
                "MCP server does not advertise required tool(s): "
                + ", ".join(sorted(result.missing_required_names))
                + ".",
            )

        for model_facing, sid, upstream_name in result.route_entries:
            if model_facing in self._route_map:
                raise McpStartupError(
                    "mcp_tool_name_collision",
                    server_id,
                    f"Duplicate MCP tool name: {model_facing}.",
                )
            self._route_map[model_facing] = (sid, upstream_name)
            self._trace.emit(
                build_event(
                    "mcp_tool_admitted",
                    run_id=self._run_id,
                    server=server_id,
                    tool=model_facing,
                )
            )

        self._specs.extend(result.admitted_specs)
        self._counts[server_id] = len(result.admitted_specs)
        self._filtered_counts[server_id] = len(result.filtered_names)

        # One bounded summary event rather than one event per filtered tool:
        # the real upstream catalog can carry dozens of non-admitted tools,
        # and their names alone are not secrets, but a per-tool stream would
        # add startup trace volume with no behavioral value.
        if result.filtered_names:
            self._trace.emit(
                build_event(
                    "mcp_tool_filtered",
                    run_id=self._run_id,
                    server=server_id,
                    filtered_count=len(result.filtered_names),
                    filtered_preview=list(result.filtered_names[:20]),
                )
            )

        self._trace.emit(
            build_event(
                "mcp_server_ready",
                run_id=self._run_id,
                server=server_id,
                discovered_count=len(tools),
                admitted_count=len(result.admitted_specs),
                filtered_count=len(result.filtered_names),
                required_count=len(cfg.required_tools),
                startup_duration_ms=int((time.monotonic() - started_at) * 1000),
            )
        )

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
            async with stdio_client(params, errlog=self._child_errlog()) as (
                read_stream,
                write_stream,
            ):
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
            # start() may have failed before the loop existed, or close() may be
            # running twice; either way the log handle is ours to release.
            self._close_errlog()
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

        # Last: every child is reaped by now, so nothing can still be writing.
        self._close_errlog()

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
        """One human-readable line per *configured* server, disabled or not.

        A server with no allowlist (``allowed_tools is None``, e.g. the
        trusted local ``time`` reference server) reports a plain tool count,
        matching its historical unfiltered wording. A server with an
        allowlist (any external server, e.g. Tracker) reports admitted and
        filtered counts so an operator can see the policy boundary at a
        glance.
        """

        summaries = []
        for server_id, cfg in self._servers_config.items():
            if not cfg.enabled:
                summaries.append(f"{server_id}: disabled")
                continue
            admitted = self._counts.get(server_id, 0)
            if cfg.allowed_tools is None:
                noun = "tool" if admitted == 1 else "tools"
                summaries.append(f"connected: {server_id} ({admitted} {noun})")
            else:
                filtered = self._filtered_counts.get(server_id, 0)
                summaries.append(
                    f"connected: {server_id} ({admitted} admitted, {filtered} filtered)"
                )
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
            ).result(timeout=self._call_timeout)
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
