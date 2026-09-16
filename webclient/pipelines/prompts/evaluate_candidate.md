Dataset wanted: $description.$fields_line
Candidate URL: $candidate_url
Detected flags (name: confidence): $flag_map_json
Observed data endpoints (XHR/fetch): $endpoints_json
Page skeleton:
$skeleton

Assess this page as the source to scrape and reply with only a JSON object with keys: "dataset_present" (bool), "is_queryable" (bool: is there an API/endpoint serving the WHOLE dataset?), "sort_order" (str|null), "completeness" ("full"|"partial"|"unknown"), "has_pagination" (bool), "has_filters" (bool), "dataset_is_subset" (bool: is our ask a subset of what is here?), "mostly_unstructured" (bool), "drilldown_links" (bool), "scrapability" (int 0-10), "verdict" (one short sentence).
