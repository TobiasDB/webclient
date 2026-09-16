You are diagnosing a FAILED data-onboarding run. The dataset wanted was: $description.$fields_line

The run reported: $reason
How far it got: $reached

The step trace:
$trace

Diagnose the failure:
- What is the MOST LIKELY root cause, given where it stopped (bad search seeds? the crawl never reached the source? the wrong page was selected? the source is client-rendered / behind login / anti-bot? the query couldn't match the content?)?
- What single change would most likely fix it?

Reply with ONLY a JSON object: "verdict" (the stage that failed: "search"|"crawl"|"select"|"evaluate"|"query"|"access"), "score" (0-10, confidence in this diagnosis), "issues" (a list of concrete suggested fixes, most likely first), "summary" (one sentence naming the likely cause).
