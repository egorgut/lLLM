# The lLLM sandbox image (SPEC-015).
#
# Built locally by `python scripts/build_sandbox_image.py`; never pulled from a
# registry at job time, and never rebuilt while a job runs.
#
# The base is pinned by digest rather than by tag: a tag is mutable, so
# `python:3.12-slim-bookworm` could point at different content tomorrow, and the
# whole isolation argument rests on knowing exactly what is inside. The digest
# below and the versions it contains are recorded in this directory's README.
#
# Debian slim already ships without curl, wget, git, ssh, and compilers, so
# there is nothing to remove here — their absence is defense in depth, while the
# authoritative controls remain the runtime policy (no network, read-only root,
# non-root user, dropped capabilities, resource limits) applied by
# sandbox_runtime/docker_backend.py.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# The fixed identity every untrusted job runs as. `--no-create-home` plus a
# nologin shell keeps the account free of a writable home and of any interactive
# entry point; the job's HOME is the bounded output tmpfs instead.
RUN groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --home-dir /sandbox/output \
       --no-create-home --shell /usr/sbin/nologin nonroot \
    && mkdir -p /sandbox/source /sandbox/input /sandbox/output \
    && chmod 0755 /sandbox /sandbox/source /sandbox/input \
    && chown 65532:65532 /sandbox/output \
    && chmod 0700 /sandbox/output

# The trusted holder: a fixed idle process that keeps the container and its
# output tmpfs alive while the host runs and collects exactly one job. It never
# executes user source — the host does that through `docker exec`.
COPY hold.py /usr/local/bin/lllm-sandbox-hold
RUN chmod 0555 /usr/local/bin/lllm-sandbox-hold

# No project source, no .env, no credentials, no Docker CLI, and no dependency
# on Ollama are copied into this image. It contains only the base distribution
# and the holder above.

USER 65532:65532
WORKDIR /sandbox/output
ENTRYPOINT []
CMD ["/usr/local/bin/lllm-sandbox-hold"]
