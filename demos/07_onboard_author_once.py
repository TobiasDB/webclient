"""07 · Author once with an LLM. Run forever with none. (The headline.)

THE POINT (vs "just onboard with a Claude session + web search"): a Claude session re-reads
the page, re-reasons, and re-guesses selectors on EVERY run -- slow, non-deterministic, costly,
and a black box. webclient uses an LLM ONCE, at authoring time, to turn a page into a
validated, self-contained query blob. From then on that blob runs with NO LLM: deterministic,
free, auditable, and identical every time -- across thousands of pages on the same schema.

The authoring LLM here is SCRIPTED so the demo is deterministic offline (set ANTHROPIC_API_KEY
to author with a real model instead). The point is the SHAPE: LLM in once, blob out; blob runs
forever without the model.
"""

import os

from webclient import WebClient, from_blob
from webclient.pipelines import Brief, write_query, cheapest_model
from _site import serve, h1, kv, table

# The one query the model authors for this page-shape (scripted for an offline, deterministic
# demo). With a real model this is what it would WRITE from the skeleton -- and never again.
AUTHORED = (
    'wq.doc.select_all(".card").extract('
    'name=wq.doc.select(".title").attr("text"), '
    'price=wq.doc.select(".price").attr("data-price")).project()'
)


def scripted_llm(prompt: str) -> str:
    if "query code" in prompt or "write a query" in prompt:
        return f"here is the query:\n{AUTHORED}"
    return "{}"


def real_llm():
    from webclient.pipelines import Budget, LlmClient

    return LlmClient(budget=Budget(), model=cheapest_model())


def main() -> None:
    base = serve()
    brief = Brief(
        name="products",
        description="the shop's products",
        fields=["name", "price"],
    )
    live = os.environ.get("ANTHROPIC_API_KEY")
    llm = real_llm() if live else scripted_llm

    with WebClient() as wc:
        h1("Step 1 - author ONCE (LLM authors + the pipeline VALIDATES the query)")
        kv("authoring llm", f"real ({cheapest_model()})" if live else "scripted (offline demo)")
        artifact = write_query(f"{base}/", brief, wc=wc, llm=llm, browser=False)
        assert artifact is not None, "authoring failed"
        kv("query", artifact.describe)
        kv("tested", f"{artifact.tested}  (ran against the page: {artifact.row_count} rows)")
        kv("complete", artifact.complete)

        h1("Step 2 - the artifact is a self-contained blob (store this, throw away the LLM)")
        kv("blob", artifact.blob[:88] + " …")

        h1("Step 3 - run FOREVER with no LLM: deterministic, free, identical every time")
        for run in (1, 2, 3):
            rows = [r.model_dump() if hasattr(r, "model_dump") else r
                    for r in from_blob(artifact.blob, wc).collect()]
            kv(f"run #{run}", f"{len(rows)} rows")
        table(rows)

    h1("Why it matters (vs a Claude session per run)")
    kv("deterministic", "the blob gives the SAME rows every time; a session drifts")
    kv("cost", "LLM paid once at authoring; every production run after is free")
    kv("scale", "one brief -> author N companies -> N blobs -> typed rows at scale")
    kv("auditable", "the query is reviewable data; a chat transcript is not a pipeline")


if __name__ == "__main__":
    main()
