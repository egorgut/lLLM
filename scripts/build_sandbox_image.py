"""Build the local sandbox image used by the isolated runtime (SPEC-015).

Usage:

    python scripts/build_sandbox_image.py             # build the configured tag
    python scripts/build_sandbox_image.py --no-cache   # ignore the layer cache

This is an explicit developer setup action, and the one place in the sandbox
where the network is used at all — pulling the pinned base image. Runtime jobs
never get a network.

The script deliberately accepts no Dockerfile path, image tag, build argument,
or remote build context: the committed Dockerfile and `config.SANDBOX_IMAGE_REF`
are the whole contract, so nothing a caller types can change what gets built or
what the runtime will later execute. It is never reachable from the model, the
chat loop, or a tool.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/build_sandbox_image.py) by
# making the project root importable for `config`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import PROJECT_ROOT, SANDBOX_IMAGE_REF  # noqa: E402

IMAGE_CONTEXT = PROJECT_ROOT / "sandbox" / "image"
DOCKERFILE = IMAGE_CONTEXT / "Dockerfile"

# Generous: a cold build pulls the base image over the network.
_BUILD_TIMEOUT_SECONDS = 900
_INSPECT_TIMEOUT_SECONDS = 30


class BuildError(Exception):
    """A clear, user-facing build failure."""


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Invoke the trusted local Docker CLI with a fixed argument vector.

    Never a shell string: `shell=False` (the default) with a token list means no
    part of this command can be reinterpreted as shell syntax.
    """

    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise BuildError(
            "Docker CLI not found. Install Docker Desktop or Docker Engine."
        ) from None
    except subprocess.TimeoutExpired:
        raise BuildError(f"Docker did not respond within {timeout}s.") from None


def _require_docker() -> None:
    result = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=_INSPECT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise BuildError(
            "Docker daemon is not available. Start Docker Desktop and try again."
        )


def build(no_cache: bool) -> str:
    """Build the configured tag and return the resolved immutable image ID."""

    if not DOCKERFILE.is_file():
        raise BuildError(f"Missing committed Dockerfile: {DOCKERFILE}")

    _require_docker()

    argv = ["docker", "build", "--tag", SANDBOX_IMAGE_REF, "--file", str(DOCKERFILE)]
    if no_cache:
        argv.append("--no-cache")
    argv.append(str(IMAGE_CONTEXT))

    print(f"Building {SANDBOX_IMAGE_REF}...")
    result = _run(argv, timeout=_BUILD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        # A build failure is a developer-facing setup problem, so the real
        # Docker diagnostics are useful here — unlike at job runtime, where they
        # stay in a bounded trace field.
        sys.stderr.write(result.stderr)
        raise BuildError("Docker build failed.")

    inspect = _run(
        ["docker", "image", "inspect", SANDBOX_IMAGE_REF, "--format", "{{.Id}}"],
        timeout=_INSPECT_TIMEOUT_SECONDS,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise BuildError("The image was built but could not be inspected.")
    return inspect.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the lLLM sandbox image (SPEC-015)."
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Rebuild every layer, ignoring the Docker cache.",
    )
    args = parser.parse_args()

    try:
        image_id = build(args.no_cache)
    except BuildError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("Sandbox image ready.")
    print(f"Tag: {SANDBOX_IMAGE_REF}")
    print(f"Image ID: {image_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
