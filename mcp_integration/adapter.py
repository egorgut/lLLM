"""Pure conversions between MCP metadata/results and the host's own contracts.

Everything in this module is synchronous and holds no live SDK session, socket,
or subprocess handle. It only translates data:

- ``namespace_name`` / ``reverse_route`` map between a remote MCP tool and the
  deterministic, namespaced, model-facing name the registry exposes;
- ``to_tool_spec`` converts discovered MCP tool metadata into the existing
  ``ToolSpec``;
- ``normalize_result`` converts an SDK ``CallToolResult`` into the JSON-compatible
  envelope the rest of the application already uses.

The normalizer is deliberately generic — it is not specialized to the time tool.
"""

from __future__ import annotations

import re
from typing import Any

from tools import ToolSpec

# Model-facing MCP names follow ``mcp_<server_id>__<remote_tool_name>``. The
# double underscore separates the (simple) server id from the remote name; the
# reverse split is unambiguous as long as server ids contain no ``__``.
_NAMESPACE_PREFIX = "mcp_"
_NAMESPACE_SEPARATOR = "__"

# Must stay compatible with ToolRegistry's name rule: ^[a-z][a-z0-9_]*$.
_MODEL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# MCP output-schema support is out of scope this iteration, but ToolSpec requires
# an output_schema, so supply a valid, permissive minimum.
_PERMISSIVE_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class McpAdapterError(Exception):
    """A controlled conversion failure, carrying a stable error type."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def namespace_name(server_id: str, remote_name: str) -> str:
    """Return the deterministic model-facing name for a remote MCP tool.

    Raises ``McpAdapterError`` if the result is not a valid registry tool name.
    """

    name = f"{_NAMESPACE_PREFIX}{server_id}{_NAMESPACE_SEPARATOR}{remote_name}"
    if not _MODEL_NAME_PATTERN.fullmatch(name):
        raise McpAdapterError(
            "mcp_invalid_tool_spec",
            f"MCP tool '{remote_name}' on server '{server_id}' does not map to a "
            f"valid model-facing name: {name!r}.",
        )
    return name


def reverse_route(model_facing_name: str) -> tuple[str, str]:
    """Recover ``(server_id, remote_name)`` from a model-facing MCP name."""

    if not model_facing_name.startswith(_NAMESPACE_PREFIX):
        raise McpAdapterError(
            "mcp_invalid_tool_spec", f"Not an MCP tool name: {model_facing_name!r}."
        )
    body = model_facing_name[len(_NAMESPACE_PREFIX):]
    server_id, separator, remote_name = body.partition(_NAMESPACE_SEPARATOR)
    if not separator or not server_id or not remote_name:
        raise McpAdapterError(
            "mcp_invalid_tool_spec", f"Malformed MCP tool name: {model_facing_name!r}."
        )
    return server_id, remote_name


def to_tool_spec(model_facing_name: str, mcp_tool: Any) -> ToolSpec:
    """Convert discovered MCP tool metadata into a declarative ``ToolSpec``.

    The functional contract (name, description, input schema) comes from MCP
    discovery; only the model-facing name is host-owned. Raises
    ``McpAdapterError`` for metadata that cannot form a valid spec.
    """

    description = (mcp_tool.description or "").strip()
    if not description:
        raise McpAdapterError(
            "mcp_invalid_tool_spec",
            f"MCP tool '{mcp_tool.name}' has no description.",
        )

    input_schema = mcp_tool.inputSchema
    if not isinstance(input_schema, dict):
        raise McpAdapterError(
            "mcp_invalid_tool_spec",
            f"MCP tool '{mcp_tool.name}' has a non-object input schema.",
        )

    return ToolSpec(
        name=model_facing_name,
        description=description,
        input_schema=input_schema,
        output_schema=_PERMISSIVE_OUTPUT_SCHEMA,
    )


def normalize_result(server_id: str, remote_name: str, result: Any) -> dict[str, Any]:
    """Normalize an SDK ``CallToolResult`` into the JSON-compatible envelope.

    Success -> ``{"ok": True, "server", "tool", "data": {...}}`` (structured
    content preferred, text content as a generic fallback). A controlled tool
    error (``isError``) -> ``{"ok": False, "server", "tool", "error": {...}}``.
    No SDK objects are retained in the output.
    """

    base = {"server": server_id, "tool": remote_name}
    structured = getattr(result, "structuredContent", None)
    content = getattr(result, "content", None)

    if getattr(result, "isError", False):
        return {"ok": False, **base, "error": _extract_error(structured, content)}

    if isinstance(structured, dict):
        data: dict[str, Any] = structured
    else:
        data = {"text": _join_text(content)}
    return {"ok": True, **base, "data": data}


def _extract_error(structured: Any, content: Any) -> dict[str, str]:
    if isinstance(structured, dict) and "message" in structured:
        error_type = structured.get("type") or "mcp_tool_error"
        return {"type": str(error_type), "message": str(structured["message"])}
    text = _join_text(content)
    return {"type": "mcp_tool_error", "message": text or "The MCP tool reported an error."}


def _join_text(content: Any) -> str:
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()
