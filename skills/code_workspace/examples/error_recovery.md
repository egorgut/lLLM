# Example: Correcting a failed script

## User request

Turn this data into a JSON file grouped by category.

## Expected behavior

Route to `code_workspace`. The first `sandbox_execute` call returns
`status: "non_zero_exit"`, `exit_code: 1`, a `KeyError` in `stderr`, and an
empty `artifacts` list — the script assumed a column the data does not have.
That error is visible, deterministic, and correctable, so make exactly one
retry: send the **complete** corrected source in a second `sandbox_execute`
call, not a fragment and not the same source again. The second call succeeds and
returns the artifact.

A different failure would end the turn instead of prompting a retry: a timeout
caused by genuinely excessive work, an attempt to reach the network, a missing
package, a host path, or an unavailable runtime cannot be fixed by editing the
script, and repeating the call would only spend the tool budget.

## Expected tools

- sandbox_execute
- sandbox_execute

## Expected answer properties

- Reports the final result, not the intermediate failure.
- Quotes the returned artifact path.
- Mentions the corrected assumption only if it changes how the user should read
  the result.
- Never claims the first attempt produced a file.
