---
name: sql-query-writing
description: Write clear, correct SQL — name tables and joins explicitly, bound parameters, indexes, and a way to verify the result.
source: agentskills.io / sql authoring best practice
verified: true
---

# SQL Query Writing

Write queries a reviewer can trust without running them:

1. State the tables and their relationships before the query.
2. Use explicit JOINs with the join keys named — never implicit joins.
3. Use bound parameters for every user-supplied value; quote identifiers where
   a column name could collide with a keyword.
4. Prefer the smallest SELECT that answers the question; mention an index that
   would speed it up when the table is large.
5. Give the expected result shape (columns/rows) so the output is verifiable.
6. If the query changes data, wrap it in a transaction and state the rollback
   path.

Include the exact way to run the query (client, database, connection string in
the request) when the context provides it.