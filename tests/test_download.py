"""``Document.download()``: a file behind a link as JSON-ready row data (base64 + metadata)."""

import base64
import hashlib

from webclient import WebClient
from webclient.interface import wq

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def test_download_a_pdf_behind_a_link(httpserver):
    httpserver.expect_request("/").respond_with_data('<html><body><a class="pdf" href="/files/report.pdf">report</a></body></html>', content_type="text/html")
    httpserver.expect_request("/files/report.pdf").respond_with_data(PDF, content_type="application/pdf")
    plan = (
        wq.reference(httpserver.url_for("/")).resolve()
        .extract(report=wq.doc.select("a.pdf").attr("href").resolve().download(), label=wq.doc.select("a.pdf").attr("text"))
        .project()
    )
    with WebClient() as wc:
        row = wc.execute(plan)
    f = row["report"]
    assert row["label"] == "report"
    assert f["filename"] == "report.pdf" and f["content_type"] == "application/pdf" and f["size"] == len(PDF)
    assert base64.b64decode(f["base64"]) == PDF and f["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_download_names_from_content_disposition(httpserver):
    httpserver.expect_request("/get").respond_with_data(b"a,b\n1,2\n", content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="data.csv"'})
    with WebClient() as wc:
        f = wc.fetch(httpserver.url_for("/get")).download()
    assert f["filename"] == "data.csv" and f["content_type"] == "text/csv"
