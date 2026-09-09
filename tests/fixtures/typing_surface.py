"""Every shape a caller is expected to write, checked by test_typing.py."""
from webclient import Document, Reference, WebClient, doc, el, field

wc = WebClient()
document: Document = wc.resolve("https://example.com")

# eager: wrappers unwrap explicitly, link attributes narrow
title: str = document.select("h1").attr("text").get()
link: Reference = document.select("a").attr("href")
resolved: Document = link.resolve()
count: int = len(document.select_all(".card"))
markdown: str = document.render("markdown").get()
status: int = document.status_code.get()
present: bool = document.supports("browser")

# lazy: the same signatures, with the combinators visible to the checker
plan = doc.select_all(".card").map(
    el.select("h3").attr("text").alias("title"),
    price=el.select(".price").attr("text"),
    link=el.select("a").attr("href"),
    detail=field("link").resolve().then(name=doc.attr("title")),
)
rows = plan.collect(document)
explanation: str = plan.explain()
