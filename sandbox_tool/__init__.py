"""The model-facing sandbox boundary (SPEC-016).

SPEC-015 built an isolated Python/Bash runtime and deliberately left it
host-only. This package is the one place that connects it to the agent: a single
local tool, `sandbox_execute`, a turn-scoped workspace that owns the artifacts a
turn produces, and the startup probe that omits the whole capability when Docker
or the pinned image is not there.

Everything the model can influence is the language, the source, and optional
input files. Every operational decision — image, mounts, network, resource
ceilings, timeouts, cleanup — stays in SPEC-015's host-owned policy, which this
package reads and never widens.
"""

from sandbox_tool.artifacts import ArtifactPublicationError
from sandbox_tool.capability import (
    SKILL_NAME,
    SandboxCapability,
    build_sandbox_capability,
)
from sandbox_tool.handler import create_sandbox_execute_handler
from sandbox_tool.schema import (
    SANDBOX_EXECUTE_SPEC,
    InvalidSandboxRequest,
    validate_arguments,
)
from sandbox_tool.workspace import TurnWorkspace

__all__ = [
    "ArtifactPublicationError",
    "InvalidSandboxRequest",
    "SANDBOX_EXECUTE_SPEC",
    "SKILL_NAME",
    "SandboxCapability",
    "TurnWorkspace",
    "build_sandbox_capability",
    "create_sandbox_execute_handler",
    "validate_arguments",
]
