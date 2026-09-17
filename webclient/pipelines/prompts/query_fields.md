You are extracting fields from ONE record of a dataset: $description.$fields_line

Here is the HTML of a SINGLE record (one row of the dataset). All field selectors you give are RELATIVE to this record:
$record_html

For EACH field in the target schema, say how to read it FROM THIS RECORD. A value that is not the element's visible text is in an ATTRIBUTE — read it by name (a rating in `data-rating`, a machine date in `datetime`, a link in `href`, a hidden value in `content`/`value`). Only text can be `regex`'d.

If a required field's value is NOT anywhere in this record but the record has a LINK to a detail page that would hold it, set `"resolve"` to follow that link.

Reply with ONLY a JSON object mapping each schema field to a spec, no prose:
{
  "<field>": {"selector": "<relative css/xpath>", "accessor": "text" | "attr" | "regex",
              "attr": "<attribute name, if accessor=attr>",
              "pattern": "<regex, if accessor=regex>", "group": <int, default 0>,
              "optional": true|false,
              "resolve": {"link_selector": "<selector of the <a> in this record>",
                          "selector": "<selector on the DETAIL page>", "accessor": "text"|"attr", "attr": "..."}},
  "<nested.branch>": {"branch": {"<leaf>": {<the same spec shape>}, ...}, "selector": "<branch container selector>"}
}
Rules: give a spec for EVERY field. Mark a field `"optional": true` only if the schema marks it optional AND it is genuinely absent from this record. Do NOT invent selectors that aren't in the record HTML above; use `resolve` if the value is only on a detail page. For a nested schema branch (a field with sub-fields like `price.value`), give a `"branch"` with a spec per leaf and the branch container `"selector"`.
