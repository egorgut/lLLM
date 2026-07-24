"""A fake MCP transport/session harness for deterministic `McpClientManager`
tests (SPEC-013).

`stdio_client` and `ClientSession` are both async context managers; real
usage is exactly:

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await session.list_tools()
            await session.call_tool(name, arguments)

This harness fakes both context managers so `McpClientManager.start()` and
`.call_tool()` can be exercised end-to-end with zero real subprocess or
asyncio-transport involvement. Register one spec per configured
`(command, args)` pair before calling `manager.start()`.
"""

from __future__ import annotations

from typing import Any, Callable


class FakeCallToolResult:
    def __init__(
        self,
        structuredContent: dict[str, Any] | None = None,
        content: Any = None,
        isError: bool = False,
    ) -> None:
        self.structuredContent = structuredContent
        self.content = content
        self.isError = isError


class FakeMcpTool:
    def __init__(
        self,
        name: str,
        description: str = "A test tool.",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class _FakeListToolsResult:
    def __init__(self, tools: list[FakeMcpTool]) -> None:
        self.tools = tools


class FakeSession:
    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def initialize(self) -> None:
        if self._spec.get("fail_initialize"):
            raise RuntimeError("fake initialize failure")

    async def list_tools(self) -> _FakeListToolsResult:
        if self._spec.get("fail_list_tools"):
            raise RuntimeError("fake list_tools failure")
        return _FakeListToolsResult(self._spec["tools"])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeCallToolResult:
        handler: Callable[[str, dict[str, Any]], FakeCallToolResult] | None = self._spec.get(
            "call_handler"
        )
        if handler is not None:
            return handler(name, arguments)
        return FakeCallToolResult(structuredContent={})


class _FakeTransport:
    def __init__(self, key: tuple[str, tuple[str, ...]]) -> None:
        self._key = key

    async def __aenter__(self) -> tuple[Any, Any]:
        # The key itself doubles as both "streams" -- the fake ClientSession
        # only needs it to look its spec back up, never to do real I/O.
        return (self._key, self._key)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeMcpEnvironment:
    """Monkeypatches `stdio_client`/`ClientSession` in `mcp_integration.client`.

    Construct with `monkeypatch`, register a spec per configured server, then
    exercise a real `McpClientManager` against it.
    """

    def __init__(self, monkeypatch) -> None:
        self._specs_by_key: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        self.stdio_client_calls: list[tuple[str, tuple[str, ...]]] = []
        self.session_calls: list[tuple[str, tuple[str, ...]]] = []
        monkeypatch.setattr("mcp_integration.client.stdio_client", self._fake_stdio_client)
        monkeypatch.setattr("mcp_integration.client.ClientSession", self._fake_client_session)

    def register(
        self,
        command: str,
        args: list[str] | tuple[str, ...],
        *,
        tools: list[FakeMcpTool],
        fail_initialize: bool = False,
        fail_list_tools: bool = False,
        call_handler: Callable[[str, dict[str, Any]], FakeCallToolResult] | None = None,
    ) -> None:
        key = (command, tuple(args))
        self._specs_by_key[key] = {
            "tools": tools,
            "fail_initialize": fail_initialize,
            "fail_list_tools": fail_list_tools,
            "call_handler": call_handler,
        }

    def _fake_stdio_client(self, params: Any) -> _FakeTransport:
        key = (params.command, tuple(params.args))
        self.stdio_client_calls.append(key)
        return _FakeTransport(key)

    def _fake_client_session(self, read_stream: Any, write_stream: Any) -> FakeSession:
        key = read_stream
        self.session_calls.append(key)
        spec = self._specs_by_key.get(key)
        if spec is None:
            raise AssertionError(f"FakeMcpEnvironment has no spec registered for {key}")
        return FakeSession(spec)
