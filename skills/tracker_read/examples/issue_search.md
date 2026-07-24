# Example: Issue search

## User request

Find open issues in queue DATA assigned to me.

## Expected behavior

Route to `tracker_read`. Call `mcp_tracker__issues_find` with a narrow query
scoped to the named queue and status (e.g. `Queue: DATA AND Status: !Closed
AND Assignee: me()`), a bounded `fields` list limited to what the answer
needs (e.g. key, summary, status, assignee, updatedAt), `include_description:
false`, and a conservative `per_page`. Summarise the returned issues rather
than reproducing the raw tool result.

## Expected tools

- mcp_tracker__issues_find

## Expected answer properties

- Lists the returned issues by key and summary.
- Does not request or display full descriptions.
- Notes if the result may be incomplete (more pages available) rather than
  implying it is exhaustive.
