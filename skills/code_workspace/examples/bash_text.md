# Example: Bash summarising supplied text files

## User request

Here are two short logs. Count the lines in each and write me a summary file.

## Expected behavior

Route to `code_workspace`. Pass both logs as `input_files` in the same call.
Write one Bash script that iterates over `/sandbox/input/*`, counts lines with
`wc -l`, writes the tally to `/sandbox/output/summary.txt`, and prints the same
tally to stdout. Bash is the right choice here because the task is plain file
enumeration and line counting; a Python script would add nothing.

## Expected tools

- sandbox_execute

## Expected answer properties

- Gives the line count for each supplied file.
- Quotes the returned path of `summary.txt`.
- Does not claim access to any file the user did not supply.
