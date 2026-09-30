$guide

----
Build the query that extracts this dataset from the page ONE STEP AT A TIME: $description.$fields_line$pager

OPS -- each turn, reply with EXACTLY ONE call (no prose, no code fence):
  records("<css>")                 pick the repeating record -- one match per row (REQUIRED first; on JSON a dotted path)
  field(<name>, <wq.doc chain>)    add (or replace) a column read INSIDE each record -- there wq.doc IS the record
  detail("<link css>")             follow each record's link ONCE (that element's href); the result shows a typical detail page
  detail_field(<name>, <chain>)    a column read on the record's DETAIL page -- there wq.doc IS the detail page
  where(<predicate>)               keep only the records matching a predicate (see filtering above): wq.doc.select(...) reads inside the record, wq.field("<col>") reads a column already added -- never a bare column name
  drop(<name>)                     remove a column
  absent(<name>)                   the field is NOT on this page nor its detail page -- say so instead of guessing a selector
  section("<css>")                 the dataset CONTINUES in another section of this page with a DIFFERENT record shape (e.g. an "Upcoming" tab vs a "Past" list): start a new section rooted at that record selector, add its fields, then done() -- the pipeline concatenates every section's rows. (Sections that SHARE a record shape need no section(): one grouped selector "a.x, b.y" in records(...) does it.)
  done()                           the query is complete: every required field added (or declared absent) and reading real values

After every call you get its RESULT -- how many records matched, the FIRST record's structure, and the values each column reads on the first records -- plus the QUERY SO FAR. Read the result before the next call: a column that reads EMPTY is reverted and needs a different selector inside the record or an attribute read (.attr("datetime"), .attr("href"), ...); a record selector that matched the wrong things is re-picked with another records(...); a field that is genuinely absent on SOME records is read with select(css, optional=True). Select the element or attribute that holds JUST the value; .regex(...) over a bigger text is a last resort for a value with no element of its own, never a substitute for finding the element. Write selectors ONLY for elements that are in the structure shown -- never invent a class or attribute; if a required field is simply not there (nor on the detail page), reply absent(<name>). Use the verbs in the guide above and no others. Never repeat a call you already made -- its result will not change.

Page skeleton:
$skeleton$hints$recency

Start with $start
