---
name: code_workspace
description: Run isolated Python or Bash to process supplied data, perform bounded computation, and create text, CSV, JSON, or Markdown files the user can open
version: "1"
allowed_tools:
  - sandbox_execute
---

# Code Workspace

## Use when

Use this skill when the task genuinely needs code to run: transforming supplied
input files, computing something too broad for a single expression, generating a
structured file the user will open, or deterministically reshaping text or
tabular data.

## Do not use when

Do not use this skill for ordinary explanation, for arithmetic a calculator
already answers, for questions about the sales database, for writing prose that
produces no file, or for anything needing the internet, package installation,
the host machine, Docker, or a long-running background job. Prefer a direct
answer whenever code would add nothing.

## Input

Identify:
- what the result must contain;
- which supplied data the script needs;
- whether the user wants a file, an answer, or both;
- the output format when a file is requested.

Ask one concise clarification when a required element is absent and cannot be
safely inferred.

## Available tools

- `sandbox_execute` runs one complete script in an isolated container and
  returns its status, exit code, bounded stdout and stderr, and metadata for any
  files it produced.

The sandbox layout is fixed:
- supplied input files are read from `/sandbox/input/<name>`;
- files for the user are written to `/sandbox/output/`, which is also the
  working directory;
- there is no network, no package installation, and no host filesystem.

Only the Python standard library and the commands already in the image are
available.

Each run starts empty. The only files that exist are the ones passed as
`input_files` in that same call: not a file an earlier run produced, not a file
from an earlier turn, and not an artifact path returned to the user before. When
the task needs data that already went out as an artifact, either recreate it
inside this script or ask the user to supply it again.

## Procedure

1. Decide whether code is actually needed; answer directly when it is not.
2. Check what data the task needs. If it refers to a file from an earlier turn —
   "that CSV", "the file you just made", a path from a previous answer — that
   file does not exist here. Decide now, before writing anything: recreate the
   data inside this script, or ask the user for it. Never write a script that
   opens it.
3. Choose Python or Bash.
4. Write the smallest complete script that solves the task.
5. Read inputs only from `/sandbox/input/`, and pass them in the same call
   through `input_files`.
6. Write every user-facing file to `/sandbox/output/`.
7. Print a short confirmation of what the script did.
8. Call `sandbox_execute` with the language, the full source, and any input
   files.
9. Read `status`, `exit_code`, `stdout`, `stderr`, and `artifacts` before
   concluding anything.
10. Retry only when the error is plausibly correctable — a syntax error, a wrong
    input name, a wrong output path, a visible logic mistake — and send the
    complete corrected source rather than a fragment.
11. Do not retry a failure no script can fix: a missing file that belongs to an
    earlier turn or an earlier run, a network or package requirement, a host
    path, or an unavailable runtime. Explain the limitation and offer to
    regenerate the data or to take it from the user instead.
12. Never repeat an identical failed call.
13. Aim to finish within two sandbox calls, leaving budget for the final answer.
14. Stop and explain instead of starting a run that cannot reasonably finish.
15. Return the result, any relevant limitation, and the name and path of every
    file that was created.

## Constraints

- Never claim a file exists unless it appears in `artifacts`.
- Never invent an output path; quote the `path` the tool returned.
- Never present a non-zero exit or a timeout as a success.
- Never use Bash merely to invoke Python, or Python merely to call a host
  command that does not exist in the image.
- Never ask for, or claim to have, access to Docker, the host filesystem, or
  the internet.
- Never try to open a file from an earlier turn or an earlier run, however the
  user refers to it — "that CSV", "the file you just made", a path quoted back
  from a previous answer. It is not there, and no path spelling makes it
  appear. Say so, and offer to recreate it or to take the data from the user.
- Do not dump long output into the answer; summarise it.
- Do not expose raw chain-of-thought.

## Completion criteria

Return:
- the result or a summary of what the script produced;
- the list of created files with their returned paths;
- the format of each file when it matters;
- any limitation, truncation, or assumption that affects the result.

When the task cannot be completed in the sandbox, say so explicitly and explain
which restriction prevented it.
