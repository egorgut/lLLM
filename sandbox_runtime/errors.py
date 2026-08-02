"""Typed host/runtime failures for the sandbox (SPEC-015 §22, "Error taxonomy").

The split is deliberate and load-bearing:

* An *untrusted job* misbehaving — raising, exiting non-zero, looping past its
  deadline, flooding stdout — is ordinary data. It comes back as a
  :class:`~sandbox_runtime.models.SandboxResult` with a non-completed status.
* A *host* defect — no Docker CLI, no daemon, no image, unparseable Docker
  output — is an exception, because the caller cannot act on it as a job result.

Public messages are short, stable, and sanitised: no host absolute path, no job
source, no input or artifact bytes, no environment value, no raw unbounded
Docker stderr. Raw Docker diagnostics belong in a bounded trace field
(``docker_stderr_preview``), never in the exception string a user might see.
"""


class SandboxError(Exception):
    """Base class for every host-side sandbox failure."""


class SandboxUnavailable(SandboxError):
    """Docker itself cannot be used: CLI missing, or the daemon is not running."""

    def __init__(self, message: str = "Docker sandbox is unavailable.") -> None:
        super().__init__(message)


class SandboxImageUnavailable(SandboxError):
    """The configured sandbox image is not present or cannot be inspected."""

    def __init__(
        self,
        message: str = (
            "Build the sandbox image with 'python scripts/build_sandbox_image.py'."
        ),
    ) -> None:
        super().__init__(message)


class SandboxRuntimeError(SandboxError):
    """A controlled failure of the runtime itself while handling one job.

    Examples: the holder container disappeared before its output could be
    copied, or Docker returned output the backend cannot parse. The job may
    have been fine; the *runtime* could not complete its contract.
    """
