"""Local MCP server exposing a single `get_current_time` tool over stdio.

This runs as a separate child process launched by the lLLM host (SPEC-009). It
speaks the Model Context Protocol on stdin/stdout via the official SDK, so
**stdout is reserved for protocol traffic** — diagnostics must never be printed
here; if needed they go to stderr only.

The tool resolves the current time using the Python standard library
(`datetime` + `zoneinfo`) with no network call, shell command, or external time
service. Success and controlled failure are both returned as MCP
``CallToolResult`` objects with structured content, so the host can normalize
them generically; an unknown timezone yields a controlled ``invalid_timezone``
error rather than a raised traceback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "time"
TOOL_NAME = "get_current_time"

# The authoritative input contract for the tool. The host discovers this through
# `tools/list` and converts it into its own ToolSpec — it is defined once, here.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": "IANA timezone name such as UTC or Europe/Amsterdam",
        }
    },
    "required": ["timezone"],
    "additionalProperties": False,
}

server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=TOOL_NAME,
            description=(
                "Return the current date and time for a given IANA timezone. Use "
                "it whenever the user asks what the current time or date is, "
                "optionally for a specific place or timezone."
            ),
            inputSchema=INPUT_SCHEMA,
        )
    ]


def _success(timezone: str, moment: datetime) -> types.CallToolResult:
    data = {"timezone": timezone, "datetime": moment.isoformat()}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=data["datetime"])],
        structuredContent=data,
        isError=False,
    )


def _failure(error_type: str, message: str) -> types.CallToolResult:
    error = {"type": error_type, "message": message}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        structuredContent=error,
        isError=True,
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    if name != TOOL_NAME:
        return _failure("unknown_tool", f"Unknown tool: {name}")

    timezone = arguments.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        return _failure("invalid_arguments", "A non-empty 'timezone' string is required.")

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return _failure("invalid_timezone", f"Unknown IANA timezone: {timezone}")

    # Seconds are kept for readability; microseconds are dropped for stable output.
    moment = datetime.now(tz=zone).replace(microsecond=0)
    return _success(timezone, moment)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(_run())
