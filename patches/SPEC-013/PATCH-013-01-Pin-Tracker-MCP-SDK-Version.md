# PATCH-013-01 — Pin Tracker MCP SDK Version

## Parent spec

`specs/SPEC-013-External-MCP-Yandex-Tracker-Read-Only.md`

## Problem

`python app.py` now aborts at startup whenever Tracker is enabled:

```text
ImportError: cannot import name 'FastMCP' from 'mcp.server'
MCP startup failed for server 'tracker': The MCP server could not be started.
```

SPEC-013 pins the Tracker server itself to an exact tested release
(`TRACKER_MCP_PACKAGE = "yandex-tracker-mcp==0.7.2"`, enforced by
`_validate_pinned_package` and by a committed-constant test). That pin is
intact — the defect is one level down.

`yandex-tracker-mcp==0.7.2` declares its own dependency as `mcp[cli]>=1.21`,
with **no upper bound**. `uvx` resolves the child environment independently of
this project, so when the MCP Python SDK published `2.0.0`, the child picked it
up. The 2.x SDK removed the `mcp.server.fastmcp` module the server imports, and
the child process dies before the session is ever initialized.

The result is that a step whose entire purpose was a *reproducible* external
integration is not reproducible: the same committed configuration launched a
working server in July and a crashing one in August, with no change on our side.

Two further points sharpen the diagnosis:

- This project's own `requirements.txt` already pins the host-side SDK to
  `mcp>=1.27,<2`. The host and the child were on different SDK generations —
  the child on a major version the host deliberately excludes.
- Upstream fixed the metadata in `0.7.3` (`mcp[cli]<2,>=1.21`). That would also
  restore startup, but leaves the guarantee resting on third-party metadata
  staying correct, and pulls in upstream changes unrelated to this defect.

## Expected change

Constrain the MCP SDK inside the Tracker child environment to the same range the
host already uses, by adding one `--with` bound to the `uvx` argument vector:

```python
TRACKER_MCP_ARGS = [
    "--from", TRACKER_MCP_PACKAGE,
    "--with", "mcp>=1.27,<2",
    "yandex-tracker-mcp",
]
```

`TRACKER_MCP_PACKAGE` stays at `0.7.2` — the release SPEC-013 tested and
journaled. The change closes the actual hole (an unpinned transitive
dependency) rather than moving to a different third-party version because its
metadata happens to be better today.

## Constraints

- Preserve SPEC-013's architecture: least-privilege allowlist, disabled by
  default, secrets read only in `mcp_integration/config.py`.
- Do not change `TRACKER_MCP_PACKAGE`, the required-tools set, the allowlist, or
  the `tracker_read` skill.
- Do not change the startup validation contract or its error taxonomy.
- Keep the constraint in host-owned configuration; the model never sees it.
- Framework-free; no new dependency.

## Acceptance criteria

- With Tracker enabled, `python app.py` starts, connects, and reports the
  admitted/filtered counts as SPEC-013 specifies.
- The child environment resolves an SDK in `>=1.27,<2`.
- The four required tools (`issue_get`, `issues_find`, `queue_get_metadata`,
  `issue_get_comments`) are still advertised and admitted; everything else is
  still filtered.
- A regression test asserts the committed argument vector carries an upper-bounded
  MCP constraint, so a future edit cannot silently drop it.
- The existing pinned-package tests continue to pass unmodified.
- The full `pytest` suite passes.
- One live-model turn drives a real Tracker read end to end.

## Files likely affected

- `config.py` — `TRACKER_MCP_ARGS`.
- `tests/test_mcp_config.py` — regression test for the committed vector.
- `README.md` — the Tracker section's configuration excerpt.
- `docs/journal/SPEC-013-tracker-mcp.md` — `## Patches` subsection.

This list is advisory, not restrictive.

## Verification

- `python -m pytest tests/test_mcp_config.py -q`
- `python -m pytest -q` (full suite, confirm no regression elsewhere)
- Live startup with Tracker enabled: confirm `connected: tracker (4 admitted,
  35 filtered)` rather than a startup abort.
- Live-model turn: ask a question that requires a Tracker read, and confirm the
  model selects the `tracker_read` skill and receives a real result.

Live-model verification *is* required here: the defect makes four model-facing
tools and one skill unavailable, so the fix changes which tools the model can
select.

## Journal strategy

Append a `## Patches` subsection to the parent journal,
`docs/journal/SPEC-013-tracker-mcp.md`. The change is a deterministic
dependency constraint; it restores the model-facing surface SPEC-013 already
specified and journaled rather than altering prompts, history construction, or
loop semantics, so no standalone PATCH journal is warranted.

## Out of scope

- Upgrading to `yandex-tracker-mcp==0.7.3` or any later release. Considered and
  deliberately rejected above; a future upgrade is its own PATCH, with its own
  live verification of the tool contract.
- Migrating this project to the MCP 2.x SDK (`requirements.txt` host-side pin).
  That is a real future step — a new SDK major version affects
  `mcp_integration/client.py` directly — and it belongs in a SPEC, not here.
- Any change to the allowlist, the required-tools set, or the `tracker_read`
  skill.
- A general mechanism for pinning transitive dependencies of other MCP servers.
  Only Tracker is affected; the local `time` server runs on `sys.executable` in
  this project's own virtual environment.
