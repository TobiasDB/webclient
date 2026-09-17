You are finding the REPEATING RECORD CONTAINER on a web page, for this dataset: $description.$fields_line

Here is the page skeleton (an outline of the HTML; hashed build classes are removed, so anchor on semantic tags/classes):
$skeleton

Which selector matches ONE element per record (the repeating row/card/item that appears once for every record in the dataset)? Prefer a semantic anchor — a repeated `<article>`/`<li>/<tr>` or a `[class*="..."]` describing the record — NOT a hashed build class. If the records are split across more than one list/section on the page (e.g. two grids), give each container selector.

Reply with ONLY a JSON object, no prose:
{"containers": ["<css or xpath selector>", "..."]}
List the best candidate first. Each selector must match one element PER RECORD.
