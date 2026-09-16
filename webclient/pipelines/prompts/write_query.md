$guide

----
Using ONLY the query DSL above, write a lazy web query that extracts this dataset from the page: $description.$fields_line$pager
Base your CSS selectors on this page skeleton:
$skeleton

Author the query rooted at wq.ref.resolve(), select the repeating records, extract the target fields, and end with .project(). Reply with ONLY the query's portable blob from expr.to_blob() -- a single JSON object, nothing else.
