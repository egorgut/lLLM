import copy
import re
from dataclasses import dataclass
from typing import Any


JsonSchema = dict[str, Any]

# A tool name is the stable, machine-facing identifier used for lookup and, in a
# later iteration, for model tool calls. Lowercase ASCII, must start with a letter.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ToolSpec:
    """An immutable description of a single tool's contract.

    This is metadata only: what the tool is called, what it does, and the shape
    of its arguments and result. It holds no callable, no Ollama objects, and no
    execution logic — those belong to a later tool-execution iteration.
    """

    name: str
    description: str
    input_schema: JsonSchema
    output_schema: JsonSchema


class ToolRegistry:
    """The single source of truth for the tool contracts an app exposes.

    A registry validates every definition at registration time, preserves
    registration order, rejects duplicates, and can render its tools into the
    function-tool format Ollama expects. It never executes a tool and never talks
    to Ollama itself.
    """

    def __init__(self) -> None:
        # A dict preserves insertion order, which gives deterministic enumeration
        # and reproducible Ollama tool lists.
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Validate a tool contract and add it to the registry.

        Raises ``TypeError`` for a non-``ToolSpec`` argument, ``ValueError`` for a
        malformed definition or a duplicate name. On success the tool becomes
        available by name and appears once, last, in registration order.
        """

        if not isinstance(spec, ToolSpec):
            raise TypeError(f"register() expects a ToolSpec, got {type(spec).__name__}.")

        _validate_name(spec.name)
        _validate_description(spec.description)
        _validate_schema(spec.input_schema, "input_schema")
        _validate_schema(spec.output_schema, "output_schema")

        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' is already registered.")

        # Store an independent copy so later mutation of the caller's schema dicts
        # cannot change the registered contract.
        self._tools[spec.name] = ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=copy.deepcopy(spec.input_schema),
            output_schema=copy.deepcopy(spec.output_schema),
        )

    def get(self, name: str) -> ToolSpec:
        """Return the registered tool for an exact name.

        Raises ``KeyError`` for an unknown name rather than returning ``None``, so
        a configuration error fails close to its source.
        """

        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool: {name}") from None

    def list_tools(self) -> tuple[ToolSpec, ...]:
        """Return all registered tools in registration order.

        A tuple is returned so callers cannot mutate the registry's state through
        the result.
        """

        return tuple(self._tools.values())

    def to_ollama_tools(self) -> list[dict[str, Any]]:
        """Render the registry as Ollama function-tool declarations.

        One declaration per tool, in registration order. The output schema is
        internal metadata and is not included. Each ``parameters`` payload is a
        fresh deep copy, so a caller mutating the result cannot corrupt the
        registry. This prepares data only — it never calls Ollama.
        """

        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": copy.deepcopy(spec.input_schema),
                },
            }
            for spec in self._tools.values()
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def _validate_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"Tool name must be a string, got {type(name).__name__}.")
    if not name:
        raise ValueError("Tool name must not be empty.")
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid tool name: {name!r}. Names must match {_NAME_PATTERN.pattern} "
            "(lowercase letter first, then lowercase letters, digits, or underscores)."
        )


def _validate_description(description: str) -> None:
    if not isinstance(description, str):
        raise TypeError(
            f"Tool description must be a string, got {type(description).__name__}."
        )
    if not description.strip():
        raise ValueError("Tool description must not be empty.")
    if description != description.strip():
        raise ValueError("Tool description must not have leading or trailing whitespace.")


def _validate_schema(schema: JsonSchema, field: str) -> None:
    """Check the top-level shape of a JSON-Schema-like contract.

    This intentionally does not implement recursive JSON Schema validation — it
    only catches malformed top-level contracts early, at registration time.
    """

    if not isinstance(schema, dict):
        raise ValueError(f"{field} must be a dict, got {type(schema).__name__}.")
    if schema.get("type") != "object":
        raise ValueError(f"{field} top-level 'type' must be 'object'.")

    properties = schema.get("properties")
    if properties is None:
        raise ValueError(f"{field} must define 'properties'.")
    if not isinstance(properties, dict):
        raise ValueError(f"{field} 'properties' must be a dict.")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise ValueError(f"{field} 'required' must be a list of strings.")

    if "additionalProperties" in schema:
        if not isinstance(schema["additionalProperties"], bool):
            raise ValueError(f"{field} 'additionalProperties' must be a boolean.")
