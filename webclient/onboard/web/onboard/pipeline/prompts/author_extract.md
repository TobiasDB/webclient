GOAL: extract these fields from each record of: $goal
FIELDS:
$schema

What is known about this source:
$source

This is $kind. The document's structure (chrome dropped, clipped):
$skeleton
$hint
The dataset wanted is the LATEST data: when records sit under year tabs / filters or a "current" and an "archive" section, author the CURRENT (latest) section. Include older sections only when the SAME record selector covers them with the same fields; an older year in a different container or format is left out.
Today is $today. Reply with JSON only:
{"records": "<the repeating record element / the record array path>", "where": "<optional: a wq.doc predicate that keeps only THIS query's records, e.g. wq.doc.attr('StartDate').datetime() >= '$today' or wq.doc.select('time').attr('datetime') < '$today'; omit when every record belongs>", "fields": {"<field>": {"css": "<selector relative to the record>" | "key": "<json key>", "read": "text" | "href" | "src" | "datetime" | "number" | "attr:<name>"}}}
Name every field you can read from a record; leave out a field the record does not hold. When THIS query's records are genuinely not on this source (an upcoming section with no events, a feed that holds only the past), reply {"none": true, "why": "<reason>"}.

Good selectors name WHAT an element is: a tag plus a semantic class or attribute (`li.release`, `h3 a`, `time[datetime]`, `a[href*="/release/"]`). The record selector names the element that repeats ONCE per record (not its container, not a nav item); a field is read RELATIVE to it. Never an id, never a position like :nth-child (except a table column), never a generated class hash -- use its stable part: `[class*="ListItem"]`. A link is read with "href" on the <a>, a date on <time> with "attr:datetime" or from its text with "datetime", a number with "number". Prefer the element that holds ONLY that value.$note
