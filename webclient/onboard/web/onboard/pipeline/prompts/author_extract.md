GOAL: extract these fields from each record of: $goal
FIELDS:
$schema

Each record is one $kind matched by $records ($count on the page). A typical record's structure:
$structure
$hint
Reply with JSON only -- for every field you can read from the record (leave out a field the record does not hold):
{"fields": {"<field>": {"css": "<selector relative to the record>" | "key": "<json key>", "read": "text" | "href" | "src" | "datetime" | "number" | "attr:<name>"}}}

Good selectors name WHAT the element is: a tag plus a semantic class or attribute (`h3 a`, `time[datetime]`, `.price`, `a[href*="/release/"]`), read RELATIVE to the record. Never an id, never a position like :nth-child (except a table column), never a generated class hash -- use its stable part: `[class*="Title"]`. A link is read with "href" on the <a>, a date on <time> with "attr:datetime" or from its text with "datetime", a number with "number". Prefer the element that holds ONLY that value.$note
