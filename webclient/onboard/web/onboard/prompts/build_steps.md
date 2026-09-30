$guide

----
Build the query that extracts this dataset from the page ONE STEP AT A TIME: $description.$fields_line$pager

OPS -- each turn, reply with EXACTLY ONE call (no prose, no code fence):
  records("<css>")                 pick the repeating record -- one match per row (REQUIRED first; on JSON a dotted path)
  field(<name>, <wq.doc chain>)    add (or replace) a column read INSIDE each record -- there wq.doc IS the record
  detail("<link css>")             follow each record's link ONCE (that element's href); the result shows the detail page
  detail_field(<name>, <chain>)    a column read on the record's DETAIL page -- there wq.doc IS the detail page
  where(<predicate>)               keep only the records matching a predicate (see filtering above)
  drop(<name>)                     remove a column
  done()                           the query is complete: every required field added and reading real values

After every call you get its RESULT -- how many records matched, the FIRST record's structure, and the values each column reads on the first records -- plus the QUERY SO FAR. Read the result before the next call: a column that reads EMPTY needs a different selector inside the record or an attribute read (.attr("datetime"), .attr("href"), ...); a record selector that matched the wrong things is re-picked with another records(...); a field that is genuinely absent on some records is read with select(css, optional=True). Extract ONLY what the skeleton shows; a required field that only exists on each record's own page is reached with detail("<link css>") and then detail_field(...) -- never by guessing an attribute.

Page skeleton:
$skeleton$hints$recency

Start with records("<css>") -- the element marked ← RECORD LIST is the likely row.
