# Example: Revenue by genre

## User request

Which music genre generated the most revenue, and what percentage of total
revenue did it generate?

## Expected behavior

Route to `sales_analysis`. Query invoice-line revenue (UnitPrice × Quantity)
grouped by genre with `sql_query`, identify the top genre, and compute its share
of total invoice-line revenue. The denominator is total invoice-line revenue.

## Expected tools

- sql_query

## Expected answer properties

- Names the top genre and its revenue.
- States the percentage and identifies its denominator (total revenue).
- States the calculation basis (revenue = UnitPrice × Quantity, grouped by genre).
- Notes the dataset limitation (local Chinook sample database).
