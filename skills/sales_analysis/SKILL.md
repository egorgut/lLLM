---
name: sales_analysis
description: Analyse sales, quantity, and revenue data from the available database
version: "1"
allowed_tools:
  - sql_query
  - python_calculate
---

# Sales Analysis

## Use when

Use this skill when the user asks to analyse sales, revenue, quantities, rankings,
shares, trends, or comparisons that can be answered from the available database.

## Do not use when

Do not use this skill for general database discovery, current-time questions,
arbitrary arithmetic unrelated to sales, or questions that do not require the
sales dataset.

## Input

Identify:
- the requested metric;
- the requested dimensions;
- the requested period when the dataset contains time;
- the requested comparison, ranking, or derived measure.

Ask one concise clarification when a required element is absent and cannot be
safely inferred.

## Available tools

- `sql_query` for reading and aggregating source data;
- `python_calculate` only for derived calculations that are clearer or safer
  outside SQL.

## Procedure

1. Restate the metric and grain internally before querying.
2. Use only tables and columns present in the supplied database schema.
3. Query aggregated data through `sql_query`.
4. Prefer one complete SQL query when it remains readable and verifiable.
5. Use `python_calculate` only for derived calculations not already returned by
   SQL.
6. Verify totals, denominators, ordering, and units before answering.
7. Check the tool result for `truncated`, missing rows, nulls, and errors.
8. Base the answer only on returned tool observations.
9. State important assumptions and limitations.
10. Return a concise answer followed by the calculation basis.

## Constraints

- Never invent tables, columns, periods, values, or units.
- Never imply access to data outside the available database.
- Never use a tool outside the declared allowlist.
- Never hide truncation or missing data.
- Never present a derived percentage without identifying its denominator.
- Do not expose raw chain-of-thought.

## Completion criteria

Return:
- the result;
- the metric and grouping basis;
- the calculation basis for derived values;
- important assumptions;
- truncation, missing-data, or dataset limitations.

When the request cannot be answered from available data, say so explicitly.
