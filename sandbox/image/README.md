# lLLM sandbox image (SPEC-015)

The disposable Linux image every sandbox job runs inside. It is built locally,
never pushed, and never pulled at job time.

## Build

```bash
python scripts/build_sandbox_image.py             # builds lllm-sandbox:spec-015
python scripts/build_sandbox_image.py --no-cache   # rebuild every layer
```

The build is the only part of the sandbox that uses the network (it pulls the
pinned base image). Runtime jobs run with `--network none`.

## Provenance

| Item | Value |
| ---- | ----- |
| Base image | `python:3.12-slim-bookworm` |
| Base digest | `sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b` |
| Debian | 12.15 (bookworm) |
| Python | 3.12.13 |
| Bash | 5.2.15(1)-release |
| GNU tar | 1.34 |
| Job user | `nonroot`, uid/gid `65532:65532` |
| Local tag | `lllm-sandbox:spec-015` (`config.SANDBOX_IMAGE_REF`) |

The base is pinned **by digest, not by tag**. A tag is mutable, so
`python:3.12-slim-bookworm` may point at different content next month; the
isolation argument depends on knowing exactly what is inside. The runtime
applies the same reasoning one level up: it resolves the local tag to an
immutable image ID with `docker image inspect` and creates containers from that
ID, so a retagged image cannot silently change what executes.

To move to a newer base, replace the digest in the `Dockerfile`, rebuild, update
this table, and record the change in the SPEC-015 journal entry.

## What is in the image

- the Debian slim base and its CPython 3.12 installation;
- `/bin/bash`, plus the ordinary core utilities the base ships;
- `/usr/local/bin/lllm-sandbox-hold` — the trusted holder (`hold.py`), the only
  project file baked in;
- `/sandbox/source`, `/sandbox/input`, `/sandbox/output` as mount points;
- the fixed non-root `nonroot` account.

## What is deliberately absent

No project source, no `.env`, no tokens, no SSH keys, no Docker CLI or socket,
no dependency on Ollama, and none of `curl`, `wget`, `git`, `ssh`, or a
compiler — Debian slim already omits those, so nothing has to be removed.

Their absence is defense in depth only. The authoritative controls are applied
at run time by `sandbox_runtime/docker_backend.py`: no network, read-only root,
all capabilities dropped, `no-new-privileges`, a fixed non-root user, and hard
memory/CPU/PID/file-descriptor/wall-time limits.

## The holder process

A container dies with its main process, taking the `/sandbox/output` tmpfs — and
everything the job generated — with it. Since there is deliberately no writable
host bind mount, the runtime needs the container to stay alive for the short
window between the job exiting and its output being collected.

So the main process is `hold.py`: an idle wait on a signal. The host runs the
untrusted script beside it with `docker exec`, collects the bounded output, then
kills the whole container. The holder itself never executes user source, reads
no input, and opens no file.

## Note on output collection

`docker cp` is **not** used to collect artifacts, despite being the obvious tool.
On Docker 29.6.1 it resolves container paths against the host-side rootfs and so
reads *nothing* from a tmpfs mount — verified with both `--tmpfs` and
`--mount type=tmpfs`, each returning an empty copy while the files were plainly
visible inside the container.

Output is therefore streamed out as a bounded `tar -cf -` over `docker exec`
(hence GNU tar in the table above), extracted under Python's `tarfile`
`filter="data"` rules, and then validated a second time by an `lstat` walk that
accepts only bounded regular files. See the SPEC-015 journal for the recorded
deviation.
