---
name: ir-events
title: Investor-relations events (upcoming + archived)
search: investor relations events calendar
max_pages: 15
prefer_api: true
schema:
  - title: {type: string, description: the event name (e.g. Q3 2025 Earnings Call, Annual Shareholder Meeting)}
  - status: {type: string, description: whether the event is UPCOMING or ARCHIVED/past (infer from the section it is listed under)}
  - datetime: {type: datetime, description: the event date and time, with timezone if shown (ISO 8601 if possible)}
  - kind: {type: string, description: the event type — earnings call, conference presentation, annual meeting, investor day, webcast}
  - location: {type: string, description: venue or "webcast/virtual"; empty if not given}
  - description: {type: string, description: the event summary/abstract, from the listing or the event's own detail page}
  - event_url: {type: url, description: the link to the event's own detail page}
  - webcast_url: {type: url, description: the live/replay webcast or registration link}
  - presentation: {type: document, description: the slide deck / presentation file (PDF/PPTX) attached to the event}
  - transcript: {type: document, description: the call transcript file (PDF) attached to the event}
  - press_release: {type: document, description: the related press release / earnings release file}
optional: [location, description, webcast_url, presentation, transcript, press_release]
---
All investor-relations events for the company — BOTH the UPCOMING events AND the ARCHIVED / past
events (the two are usually listed in separate sections of the IR events/calendar page; capture
records from both, tagging each with its `status`). For each event extract the full detail: name,
date/time, type, location, and description — following the event's own detail page (`event_url`) when
the listing only shows a summary. Also capture the attached content as downloadable documents: the
presentation/slide deck, the transcript, and the press/earnings release (these are `document`
fields — their file URLs are resolved to blobs for the object store, keyed to the event's metadata).
