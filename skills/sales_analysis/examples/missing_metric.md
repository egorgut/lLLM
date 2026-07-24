# Example: Missing metric (clarification)

## User request

Analyse sales.

## Expected behavior

Route to `sales_analysis`. The required metric and grain are absent and cannot be
safely inferred, so ask exactly one concise clarification instead of querying
arbitrary data. No tool is called. The clarification is a valid completed turn;
the user's next message is routed again.

## Expected tools

(none)

## Expected answer properties

- A single concise clarification question.
- Asks which sales metric and/or period to analyse.
- Does not invent a metric or run a query.
