"""Pure exact-name admission-filtering algorithm (SPEC-013 §"Admission algorithm").

No I/O, no session, no subprocess. This is the host-owned policy boundary that
decides which discovered upstream tools may ever become model-visible: it runs
identically for every configured server, including the trusted local ``time``
reference server (``allowed_tools=None`` preserves its historical unrestricted
behavior). An external server such as Tracker must supply a finite allowlist;
every tool not in it is filtered before any conversion happens, so a filtered
tool's schema and description are never even converted into a ``ToolSpec``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_integration.adapter import namespace_name, to_tool_spec
from tools import ToolSpec


class McpAdmissionError(Exception):
    """A controlled admission failure, carrying a stable error type."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class AdmissionResult:
    admitted_specs: tuple[ToolSpec, ...]
    # (model_facing_name, server_id, upstream_name)
    route_entries: tuple[tuple[str, str, str], ...]
    filtered_names: tuple[str, ...]
    missing_required_names: frozenset[str]


def filter_discovered_tools(
    server_id: str,
    tools: list[Any],
    *,
    allowed_tools: frozenset[str] | None,
    required_tools: frozenset[str] = frozenset(),
) -> AdmissionResult:
    """Filter raw ``tools/list`` results down to the admitted subset.

    Names are compared exactly and case-sensitively. A duplicate upstream name
    or a model-facing collision is a controlled ``McpAdmissionError``, not a
    silent overwrite. ``required_tools`` is checked against the *admitted* set
    only, so a required tool that exists upstream but is not in
    ``allowed_tools`` still counts as missing.
    """

    seen_upstream: set[str] = set()
    seen_model_facing: set[str] = set()
    admitted_specs: list[ToolSpec] = []
    route_entries: list[tuple[str, str, str]] = []
    filtered: list[str] = []
    admitted_names: set[str] = set()

    for tool in tools:
        name = tool.name
        if name in seen_upstream:
            raise McpAdmissionError(
                "mcp_tool_name_collision",
                f"Duplicate upstream tool name discovered on server "
                f"{server_id!r}: {name!r}.",
            )
        seen_upstream.add(name)

        if allowed_tools is not None and name not in allowed_tools:
            filtered.append(name)
            continue

        model_facing = namespace_name(server_id, name)
        spec = to_tool_spec(model_facing, tool)
        if model_facing in seen_model_facing:
            raise McpAdmissionError(
                "mcp_tool_name_collision", f"Duplicate MCP tool name: {model_facing}."
            )
        seen_model_facing.add(model_facing)

        admitted_specs.append(spec)
        route_entries.append((model_facing, server_id, name))
        admitted_names.add(name)

    missing = frozenset(required_tools) - admitted_names
    return AdmissionResult(
        admitted_specs=tuple(admitted_specs),
        route_entries=tuple(route_entries),
        filtered_names=tuple(sorted(filtered)),
        missing_required_names=missing,
    )
