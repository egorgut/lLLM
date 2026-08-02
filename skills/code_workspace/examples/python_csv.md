# Example: Python producing a CSV artifact

## User request

Create a CSV with the numbers 1 through 5 and their squares.

## Expected behavior

Route to `code_workspace`. Write one complete Python script that opens
`/sandbox/output/squares.csv`, writes a header row and five data rows, and
prints a short confirmation. Call `sandbox_execute` once with
`language: "python"` and no input files. The result carries
`status: "succeeded"`, `exit_code: 0`, and one artifact.

## Expected tools

- sandbox_execute

## Expected answer properties

- States that the file was created.
- Quotes the artifact path exactly as the tool returned it.
- Names the columns the file contains.
- Does not paste the script or the full file contents into the answer.
