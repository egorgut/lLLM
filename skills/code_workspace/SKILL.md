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

## Procedure

1. Decide whether code is actually needed; answer directly when it is not.
2. Choose Python or Bash.
3. Write the smallest complete script that solves the task.
4. Read inputs only from `/sandbox/input/`, and pass them in the same call
   through `input_files`.
5. Write every user-facing file to `/sandbox/output/`.
6. Print a short confirmation of what the script did.
7. Call `sandbox_execute` with the language, the full source, and any input
   files.
8. Read `status`, `exit_code`, `stdout`, `stderr`, and `artifacts` before
   concluding anything.
9. Retry only when the error is plausibly correctable, and send the complete
   corrected source rather than a fragment.
10. Never repeat an identical failed call.
11. Aim to finish within two sandbox calls, leaving budget for the final answer.
12. Stop and explain instead of starting a run that cannot reasonably finish.
13. Return the result, any relevant limitation, and the name and path of every
    file that was created.

## Constraints

- Never claim a file exists unless it appears in `artifacts`.
- Never invent an output path; quote the `path` the tool returned.
- Never present a non-zero exit or a timeout as a success.
- Never retry a failure caused by missing network access, a missing package, a
  host path, or an unavailable runtime — explain the limitation instead.
- Never use Bash merely to invoke Python, or Python merely to call a host
  command that does not exist in the image.
- Never ask for, or claim to have, access to Docker, the host filesystem, the
  internet, or files from an earlier turn.
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
