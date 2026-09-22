# API reference

Rendered from the docstrings (mkdocstrings). The typed surface is generated from the
backings (`scripts/gen_stubs.py`), so what is documented here is what runs.

## Client

::: webclient.core.client.WebClient
    options:
      members: [fetch, ref, crawl, locate, sitemap, robots, session, record, trace, driver, escalate, scripts, errors, execute]

## Document

::: webclient.core.document.Document
    options:
      members: [select, select_all, attr, markdown, text, skeleton, flags, card, patterns, paginate, extract, project, events_of, errors]

## Loops & pipelines

::: webclient.loop
::: webclient.pipeline

## Traces & replay

::: webclient.trace
::: webclient.replay

## Scripts

::: webclient.scripts

## Tools

::: webclient.tools
    options:
      members: [Tool, tool, dispatch, schema]

## Settings

::: webclient.settings.Settings
