"""Shared deterministic fixtures for agent-loop tests and scripted evaluations.

None of these require a live Ollama model, a live MCP server, or real tool
implementations. `evals/runner.py` reuses these directly (see its module
docstring) rather than duplicating small fixture classes for a handful of
scripted scenarios.
"""

from llm import ModelToolCall
from tools import ToolRegistry, ToolSpec


def make_tool_call(name: str, arguments: dict) -> ModelToolCall:
    return ModelToolCall(id=None, name=name, arguments=arguments)


def make_tool_registry(*names: str) -> ToolRegistry:
    """A real `ToolRegistry` populated with trivial tools for skill tests.

    Skill validation and tool filtering need a registry that answers `in`,
    `.get`, and declaration rendering; the tool *behavior* is irrelevant here.
    """

    registry = ToolRegistry()
    for name in names:
        registry.register(
            ToolSpec(
                name=name,
                description=f"Test tool {name}.",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {}},
            )
        )
    return registry


class ScriptedRouteFn:
    """A `RouteFn` (messages -> raw text) that plays back a fixed script.

    Each item is either a `str` to return or a `BaseException` to raise (for the
    transport-failure scenario). Every call's messages snapshot is recorded so a
    test can assert the router never received full skill instructions and that a
    repair request carried the required context.
    """

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages) -> str:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("ScriptedRouteFn ran out of scripted responses.")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ScriptedSkillRouter:
    """A stand-in `SkillRouter` for orchestrator/turn tests.

    Returns a preset `SkillSelection` or raises a preset `AgentRuntimeError`,
    recording the keyword arguments it was called with so a test can assert the
    router saw the shared deadline and bounded context.
    """

    def __init__(self, result) -> None:
        self._result = result
        self.calls: list[dict] = []

    def select(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class ScriptedModelResponse:
    """A canned `ModelResponseLike` for one model decision.

    Pass `block_on` (an unset `threading.Event`) to simulate a model request
    that never finishes streaming — the first (and only) chunk read blocks on
    the event forever, which is exactly what a real deadline-timeout test
    needs without requiring the production-sized timeout constants.
    """

    def __init__(self, *, text: str = "", tool_calls=None, block_on=None) -> None:
        self._text = text
        self._tool_calls = list(tool_calls) if tool_calls else []
        self._block_on = block_on

    def text_chunks(self):
        if self._block_on is not None:
            self._block_on.wait()
        if self._text:
            yield self._text

    @property
    def tool_calls(self) -> list[ModelToolCall]:
        return self._tool_calls


class ScriptedResponder:
    """A `Respond` callable that plays back a fixed script.

    Each scripted item is either a `ScriptedModelResponse` to return or a
    `BaseException` instance to raise (used for the user-interrupt scenario).
    Every call is recorded (a snapshot of the messages passed in, plus the
    tool declarations) so tests can assert on what the loop actually sent.
    """

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict], object]] = []

    def __call__(self, messages, tools):
        self.calls.append((list(messages), tools))
        if not self._responses:
            raise AssertionError("ScriptedResponder ran out of scripted responses.")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeToolExecutor:
    """A duck-typed stand-in for `tools.executor.ToolExecutor`.

    Handlers are plain `arguments -> result dict` callables, or a callable
    that blocks on a `threading.Event` to simulate a hung tool.
    """

    def __init__(self, handlers: dict | None = None) -> None:
        self._handlers = dict(handlers) if handlers else {}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        handler = self._handlers.get(name)
        if handler is None:
            raise AssertionError(f"FakeToolExecutor has no handler for tool: {name}")
        return handler(arguments)


class RecordingRenderer:
    """A `Renderer` that records everything instead of printing it."""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, int, int]] = []
        self.tool_results: list[dict] = []
        self.text_chunks: list[str] = []

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None:
        self.tool_calls.append((call.name, used, maximum))

    def tool_result(self, result: dict) -> None:
        self.tool_results.append(result)

    def text(self, chunk: str) -> None:
        self.text_chunks.append(chunk)


class FakeClock:
    """A manually-advanced monotonic clock for whole-turn-deadline tests.

    `AgentRunner`'s own deadline bookkeeping reads this clock, so a test can
    simulate "no turn time remains" by advancing it, with no real waiting.
    Component timeouts (`reliability.run_with_deadline`) still use real wall
    time internally — only `AgentRunner`'s deadline arithmetic is faked.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
