# Example: Issue lookup

## User request

Use the tracker_read skill and show me issue DATA-142.

## Expected behavior

Route to `tracker_read`. Call `mcp_tracker__issue_get` with `issue_id:
"DATA-142"`, requesting the description since the user wants to see the
issue. Answer using only the fields the tool actually returned.

## Expected tools

- mcp_tracker__issue_get

## Expected answer properties

- States the issue key and summary.
- States status, assignee, and queue when the tool returned them.
- Does not claim a field is absent from Tracker merely because it was not
  requested or returned.
