We searched the web for the entity named EXACTLY '$entity' (dataset wanted: $description).$fields_line

Some results may be for a DIFFERENT organisation that just has a similar name (for example "Square" / squareup.com when we asked for "Squarepoint") — those do NOT belong to '$entity'.

Search results:
$seeds

Which results are HOSTED BY '$entity' ITSELF — its own corporate website or its own investor-relations host (e.g. investors.<company>.com, ir.<company>.com, <company>.gcs-web.com, <company>.q4cdn.com)? Judge from the DOMAIN first, then the title and snippet. A page that is merely ABOUT '$entity' on someone else's site -- a news outlet's article, a market-data or filings aggregator, a directory, a conference site, a partner's announcement -- does NOT belong, however relevant it reads: we want the entity's OWN data, published by the entity. Also EXCLUDE look-alike organisations with a similar name.

Reply with ONLY a JSON object: "belong" (a list of the result NUMBERS that belong to '$entity'; [] if none of them do), "note" (one short sentence — e.g. name the look-alike you excluded).
