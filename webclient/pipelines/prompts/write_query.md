$guide

----
Using ONLY the query syntax above, write a query that extracts this dataset from the page: $description.$fields_line$pager
Base your CSS selectors on this page skeleton:
$skeleton

Root the query at wq.doc, select the repeating records with .select_all(...), extract the target fields into columns (nesting a sub-extract per nested schema branch), and end with .project(). Reply with ONLY the query's portable blob from expr.to_blob() -- a single JSON object, nothing else.
