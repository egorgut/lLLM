# Example: Queue metadata

## User request

What is the DATA queue and which defaults does it use?

## Expected behavior

Route to `tracker_read`. Call `mcp_tracker__queue_get_metadata` with
`queue_id: "DATA"` and an empty `expand` list unless the user's question
requires a specific expansion. Answer using only the returned metadata.

## Expected tools

- mcp_tracker__queue_get_metadata

## Expected answer properties

- Describes the queue using only fields the tool returned.
- Does not enumerate other queues or imply broader queue access.
