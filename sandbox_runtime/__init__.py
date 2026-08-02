"""Isolated sandbox runtime (SPEC-015).

Host-only infrastructure for executing one untrusted Python or Bash script
inside a disposable, network-less, read-only-root, non-root, resource-capped
Docker container, returning a bounded structured result.

Nothing here is model-facing. There is no ``ToolSpec``, no registry entry, no
skill, and no prompt text; ``app.py`` and the agent loop do not import this
package, and ordinary chat runs unchanged whether or not Docker is installed.
SPEC-016 will add the tool boundary on top of ``SandboxRuntime.execute``.

Typical host-side use::

    runtime = DockerSandboxRuntime(run_id=new_id())
    result = runtime.execute(
        SandboxJob(language=SandboxLanguage.PYTHON, source="print(1 + 1)")
    )
"""

from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_runtime.errors import (
    SandboxError,
    SandboxImageUnavailable,
    SandboxRuntimeError,
    SandboxUnavailable,
)
from sandbox_runtime.models import (
    SandboxArtifact,
    SandboxJob,
    SandboxLanguage,
    SandboxResult,
    SandboxRuntime,
    SandboxStatus,
)
from sandbox_runtime.policy import SandboxPolicy, default_policy, validate_sandbox_policy

__all__ = [
    "DockerSandboxRuntime",
    "SandboxArtifact",
    "SandboxError",
    "SandboxImageUnavailable",
    "SandboxJob",
    "SandboxLanguage",
    "SandboxPolicy",
    "SandboxResult",
    "SandboxRuntime",
    "SandboxRuntimeError",
    "SandboxStatus",
    "SandboxUnavailable",
    "default_policy",
    "validate_sandbox_policy",
]
