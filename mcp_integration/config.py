"""Validated MCP server configuration model and the Tracker env-var loader.

Top-level ``config.py`` intentionally contains no environment-variable
parsing — that invariant predates SPEC-013 and is preserved deliberately.
Tracker is the first MCP server whose configuration depends on the process
environment (a token, an organisation id), so all ``os.environ`` reads for it
live here, at the MCP integration boundary, never in the shared top-level
constants module.

``load_tracker_server_config`` is fail-fast and sanitised: every failure
raises the existing ``McpStartupError`` (SPEC-009's startup taxonomy, extended
with two new ``error_type`` values), and no message ever interpolates a secret
or organisation-id *value* — only variable *names*.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import config as host_config
from mcp_integration.client import McpStartupError

# Organisation id: exactly one of these two must be set when Tracker is
# enabled (SPEC-013 core decision #8).
_ORG_ID_KEYS = ("TRACKER_CLOUD_ORG_ID", "TRACKER_ORG_ID")

# Copied explicitly into the child's environment so `uvx` can find an
# interpreter and a home directory; never a full `os.environ` dump.
_PLATFORM_ENV_KEYS = ("PATH", "HOME")

_PINNED_PACKAGE_PATTERN = re.compile(r"^yandex-tracker-mcp==\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class McpServerConfig:
    """Host-owned, immutable configuration for one MCP server.

    ``allowed_tools is None`` preserves the historical unrestricted behavior
    of a trusted local reference server (e.g. ``time``); an external server
    must supply a finite, non-empty allowlist while enabled.
    """

    server_id: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    allowed_tools: frozenset[str] | None = None
    required_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.allowed_tools is not None and not self.required_tools <= self.allowed_tools:
            raise ValueError(
                f"required_tools must be a subset of allowed_tools for server "
                f"{self.server_id!r}."
            )
        if self.enabled and self.allowed_tools is not None and not self.allowed_tools:
            raise ValueError(
                f"Server {self.server_id!r} is enabled with an empty allowlist; "
                "an external server must admit at least one tool."
            )


def env_bool(name: str, *, default: bool) -> bool:
    """Parse a loosely-typed boolean env var. Raises ``ValueError`` on garbage."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Unrecognized boolean value for {name}.")


def load_tracker_server_config() -> McpServerConfig:
    """Build the Tracker ``McpServerConfig`` (SPEC-013 §"Validation order").

    Reads Tracker environment only when enabled. Returns a fully-formed
    disabled config immediately when ``TRACKER_MCP_ENABLED`` is false/absent,
    without touching ``TRACKER_TOKEN`` or any organisation-id variable.
    """

    server_id = host_config.TRACKER_MCP_SERVER_ID
    required = frozenset(host_config.TRACKER_MCP_REQUIRED_TOOLS)

    try:
        enabled = env_bool("TRACKER_MCP_ENABLED", default=False)
    except ValueError as error:
        raise McpStartupError(
            "tracker_configuration_missing", server_id, str(error)
        ) from error

    if not enabled:
        return McpServerConfig(
            server_id=server_id,
            command=host_config.TRACKER_MCP_COMMAND,
            args=tuple(host_config.TRACKER_MCP_ARGS),
            env={},
            enabled=False,
            allowed_tools=required,
            required_tools=required,
        )

    _validate_pinned_package(host_config.TRACKER_MCP_PACKAGE, server_id)
    token = _require_env("TRACKER_TOKEN", server_id)
    org_key, org_value = _require_exactly_one_org_id(server_id)

    env = {**_platform_env(), "TRACKER_TOKEN": token, "TRANSPORT": "stdio", org_key: org_value}
    return McpServerConfig(
        server_id=server_id,
        command=host_config.TRACKER_MCP_COMMAND,
        args=tuple(host_config.TRACKER_MCP_ARGS),
        env=env,
        enabled=True,
        allowed_tools=required,
        required_tools=required,
    )


def _validate_pinned_package(package: str, server_id: str) -> None:
    if not _PINNED_PACKAGE_PATTERN.match(package):
        raise McpStartupError(
            "tracker_configuration_conflict",
            server_id,
            "Tracker MCP package reference is not pinned to an exact version.",
        )


def _require_env(name: str, server_id: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise McpStartupError(
            "tracker_configuration_missing",
            server_id,
            f"Tracker MCP is enabled but {name} is missing.",
        )
    return value.strip()


def _require_exactly_one_org_id(server_id: str) -> tuple[str, str]:
    present = [
        (key, os.environ[key].strip())
        for key in _ORG_ID_KEYS
        if os.environ.get(key, "").strip()
    ]
    if not present:
        raise McpStartupError(
            "tracker_configuration_missing",
            server_id,
            "Tracker MCP is enabled but no organisation ID is set (expected "
            f"exactly one of {', '.join(_ORG_ID_KEYS)}).",
        )
    if len(present) > 1:
        raise McpStartupError(
            "tracker_configuration_conflict",
            server_id,
            "Tracker MCP requires exactly one organisation ID, but both "
            f"{', '.join(_ORG_ID_KEYS)} are set.",
        )
    return present[0]


def _platform_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _PLATFORM_ENV_KEYS if key in os.environ}
