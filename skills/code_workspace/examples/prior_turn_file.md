# Example: A request for a file from an earlier turn

## User request

(after an earlier turn produced `squares.csv`)

Now read that CSV back and tell me the sum of the squares column.

## Expected behavior

Route to `code_workspace`, then recognise **before calling the tool** that the
file is not reachable. Each run starts empty, and the artifact from the earlier
turn is not among this call's `input_files` — so no script and no path spelling
can open it. Quoting back the exact `path` from the previous answer does not
help either; that path is meaningful to the user, not inside the sandbox.

The right move is one of:

- recreate the data inside a single script and answer from it, when the data is
  something the agent can regenerate (as here — the numbers 1-5 and their
  squares are fully determined by the earlier request);
- ask the user to supply the file again, when it is not reproducible.

Either way the answer says plainly that a new turn cannot see the earlier
turn's files.

What must **not** happen is the failure being discovered by running into it:
one call returning `FileNotFoundError`, then a second call retrying with a
different path spelling. That spends two of the four tool calls on a fact the
skill already states, and a third spelling would fail exactly the same way.

## Expected tools

- sandbox_execute (once, or not at all)

## Expected answer properties

- Gives the requested result when the data can be regenerated.
- States that a later turn cannot read an earlier turn's files.
- Offers the alternative — regenerate, or the user supplies the file.
- Does not claim the earlier artifact was read.
- Does not retry the same lookup with a different path.
