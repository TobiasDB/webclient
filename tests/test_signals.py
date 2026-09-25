"""The signals detection package: isolation + extensibility.

Detection lives in ``webclient.signals`` -- a registry of small detector functions,
independent of the cores. This checks it is (a) importable and usable on its own and
(b) extensible: a new detector/flag is registered with a decorator, no dispatch code
changes.
"""

import subprocess
import sys

from webclient.core.document.models import Flag
from webclient.signals import (
    Context,
    Hit,
    detector,
    flag,
    flags,
    flags_from_response,
)
from webclient.signals.registry import DETECTORS, FLAGS


def test_flags_from_response_is_pure_and_usable_standalone():
    # the request/static half needs nothing but the response -- no fetch, no core.
    f = flags_from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>',
    )
    assert f["spa"].present and f["spa"].remedy == "browser"
    # every signal is self-describing: a stage + a confidence + a reason
    sig = f["spa"].signals[0]
    assert sig.stage in ("request", "static", "rendered", "network")
    assert 0.0 < sig.confidence <= 1.0 and sig.reason


def test_iframe_and_shadow_dom_flags_from_the_static_tree():
    # even without a render, an <iframe> element and an attachShadow/shadowrootmode marker
    # are flagged (statically) so auto knows to escalate to a browser that can inline them.
    got = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b"<html><body><main><iframe src='/f'></iframe>"
        b"<div id='h'></div><script>document.getElementById('h')"
        b".attachShadow({mode:'open'})</script></main></body></html>",
    ))
    assert got["iframe"].present and got["iframe"].value == 1  # one iframe element
    assert got["shadow_dom"].present  # attachShadow marker in the served HTML
    assert got["shadow_dom"].signals[0].stage == "static"


def test_tabbed_flag_fires_on_tabs_but_not_on_a_plain_table():
    # same-page tab controls (Upcoming/Past, year tabs) -> the tabbed flag; distinct from
    # pagination. A plain <table> must NOT trip it (avoid matching the substring "table").
    tabs = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><div role="tablist"><button role="tab">Upcoming</button>'
        b'<button role="tab">Past</button></div><div role="tabpanel"></div></body></html>',
    ))
    assert tabs["tabbed"].present and tabs["tabbed"].confidence >= 0.6
    plain = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><table class="data-table"><tr><td>x</td></tr></table></body></html>',
    ))
    assert not plain["tabbed"].present


def test_page_param_link_detection_shares_the_canon_param_table():
    # a link whose query carries a pagination param (offset/page/…) trips page_param_links; a
    # ?p=<id> post link must NOT (the WordPress guard). The table is crawl.canon's, so detection
    # and crawl's series-dedup never drift.
    paged = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><main><article>x</article>'
        b'<a href="/list?offset=20">next</a></main></body></html>',
    ))
    assert any(s.name == "page_param_links" for s in paged["pagination"].signals)  # ?offset= counts

    post = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><main><a href="/read?p=123">a post</a></main></body></html>',
    ))
    assert not any(s.name == "page_param_links" for s in post["pagination"].signals)  # ?p= is an id


def test_pagination_hint_reads_kind_name_and_totals():
    from webclient.core.document.models import PaginationHint

    f = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><main><article>x</article><p>Showing 1-20 of 348</p>'
        b'<a href="/list?page=2">next</a></main></body></html>',
    ))
    h = f["pagination"].value
    assert isinstance(h, PaginationHint)
    assert h.kind == "param" and h.name == "page"  # a page-param advance
    assert h.total_items == 348 and h.page_size == 20  # from the "1-20 of 348" caption


def test_pagination_hint_link_header_marks_an_api_listing():
    # a JSON API paginated only via the HTTP Link header (no HTML pager) still detects, kind=link
    f = flags(Context.from_response(
        200, {"content-type": "application/json", "Link": '<https://api.x/items?page=2>; rel="next"'},
        {}, b"[]",
    ))
    assert f["pagination"].present and f["pagination"].value.kind == "link"


def test_ordered_flag_reads_sort_direction_and_relevance():
    from webclient.core.document.models import Ordering

    # newest-first <time> dates + a sort control -> date / desc / controllable (a recency stop is sound)
    dated = flags(Context.from_response(
        200, {"content-type": "text/html"}, {},
        b'<html><body><select name="sort"><option>New</option></select><main>'
        b'<article><time datetime="2026-03-01">a</time></article>'
        b'<article><time datetime="2026-02-01">b</time></article>'
        b'<article><time datetime="2026-01-01">c</time></article></main></body></html>',
    ))["ordered"].value
    assert isinstance(dated, Ordering)
    assert dated.key == "date" and dated.direction == "desc" and dated.controllable

    # a search-results URL -> relevance order (no early pagination stop is sound)
    rel = flags(Context.from_response(
        200, {"content-type": "text/html"}, {}, b"<html><body>x</body></html>",
        url="http://x/search?q=coffee",
    ))["ordered"].value
    assert rel.key == "relevance"


def test_filtered_and_live_flags():
    from datetime import date, timedelta

    from webclient.core.document.models import Filtering, Liveness

    recent = (date.today() - timedelta(days=3)).isoformat()
    html = (
        f'<html><body><aside class="facet"><select name="filter_cat"></select></aside><main>'
        f'<article><time datetime="{recent}">x</time></article>'
        f'<article><time datetime="2025-01-01">y</time></article>'
        f'<article><time datetime="2024-01-01">z</time></article></main></body></html>'
    ).encode()
    f = flags(Context.from_response(
        200, {"content-type": "text/html"}, {}, html, url="http://x/list?category=news&page=2",
    ))
    filt = f["filtered"].value
    assert isinstance(filt, Filtering) and filt.active == {"category": "news"}  # page= is NOT a filter
    assert "filter_cat" in filt.controls
    live = f["live"].value
    assert isinstance(live, Liveness) and live.recent and live.drift_risk  # recent + newest-first -> drift


def test_caas_content_service_marker_fires_spa_so_auto_renders():
    # a page whose records are fetched by a content-service widget (e.g. Adobe Milo /
    # a CaaS block) leaves only a shell in the served HTML; the static marker fires spa
    # so browser="auto" knows to render it.
    shell = (b"<html><body><div class='caas' data-caas></div>"
             b"<script src='https://milo.adobe.com/libs/x.js'></script></body></html>")
    f = flags_from_response(200, {"content-type": "text/html"}, {}, shell)
    assert f["spa"].present
    assert any("client-render marker" in s.reason for s in f["spa"].signals)


def test_large_document_flag_is_content_size_based():
    # a flag that fires purely on CONTENT SIZE (independent of any skeleton), so a caller knows a
    # skeleton of the page will be trimmed/collapsed to fit a budget. Its value is the size.
    big = b"<html><body>" + b"<p>lots of real content here </p>" * 12000 + b"</body></html>"
    f = flags_from_response(200, {"content-type": "text/html"}, {}, big)
    assert f["large_document"].present and f["large_document"].value == len(big.decode())
    assert f["large_document"].signals[0].stage == "static"
    # a small page does not
    small = flags_from_response(200, {"content-type": "text/html"}, {}, b"<html><body><p>hi</p></body></html>")
    assert not small["large_document"].present


def test_is_data_endpoint_tells_content_apis_from_analytics():
    from webclient.signals.dom import _is_data_endpoint

    assert _is_data_endpoint("https://milo.adobe.com/tools/caas?complexquery=x")  # CaaS API
    assert _is_data_endpoint("https://api.other.com/v2/items.json")               # JSON API
    assert not _is_data_endpoint("https://www.google-analytics.com/collect")      # analytics
    assert not _is_data_endpoint("https://adobedtm.com/launch.min.js")            # tag manager
    assert not _is_data_endpoint("https://cdn.example.com/logo.png")              # an asset


def test_contra_evidence_pulls_a_flag_below_present():
    # negative/contra evidence: a framework-marked page that already server-renders its
    # content is NOT a client shell -- the static_content_present CONTRA detector lowers spa
    # below the present threshold, so we don't needlessly escalate an SSR page to a browser.
    ssr = (b"<html><head><script>window.__NEXT_DATA__={}</script></head><body>"
           + b"<p>real server-rendered text </p>" * 400 + b"</body></html>")
    f = flags_from_response(200, {"content-type": "text/html"}, {}, ssr)
    assert not f["spa"].present  # a positive marker fired, but contra pulled it down
    assert any(s.contra for s in f["spa"].signals)  # the contra evidence is recorded on the flag

    # an EMPTY shell with the same marker keeps spa present (contra doesn't fire)
    shell = b"<html><head><script>window.__NEXT_DATA__={}</script></head><body><div id=__next></div></body></html>"
    assert flags_from_response(200, {"content-type": "text/html"}, {}, shell)["spa"].present


def test_signals_package_imports_without_lxml_or_a_browser():
    # the detection package is isolated: importable + runnable with the native deps
    # unavailable, so a remote/thin context can read flags too.
    script = (
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('lxml', 'playwright'):\n"
        "        raise ImportError(name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "from webclient.signals import flags_from_response\n"
        "f = flags_from_response(401, {}, {}, b'unauthorized')\n"
        "assert f['login_required'].present\n"
        "import sys; assert 'lxml' not in sys.modules\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_a_new_detector_and_flag_extend_detection_without_dispatch_changes():
    # register a brand-new flag + detector; run() picks it up with no other change.
    n_det, n_flag = len(DETECTORS), len(FLAGS)
    flag("cookie_consent", remedy=None)

    @detector(flag="cookie_consent", name="consent_banner", stage="static")
    def _consent(ctx: Context) -> "Hit | None":
        return Hit(0.8, "a cookie-consent banner") if "we use cookies" in ctx.low else None

    try:
        got = flags(Context.from_response(
            200, {"content-type": "text/html"}, {},
            b"<html><body><div>We use cookies to improve your experience.</div></body></html>",
        ))
        assert isinstance(got["cookie_consent"], Flag)
        assert got["cookie_consent"].present and got["cookie_consent"].confidence == 0.8
        assert got["cookie_consent"].signals[0].name == "consent_banner"
    finally:  # keep the global registry clean for the other tests
        FLAGS.pop("cookie_consent", None)
        DETECTORS[:] = [d for d in DETECTORS if d.name != "consent_banner"]
    assert len(DETECTORS) == n_det and len(FLAGS) == n_flag  # fully removed


def test_detectors_declare_the_scripts_they_need_and_docs_are_generated():
    import subprocess, sys, pathlib
    import webclient.signals.dom  # noqa: F401
    from webclient.signals.registry import DETECTORS

    rendered = [d for d in DETECTORS if d.stage in ("rendered", "network")]
    assert rendered and all("LiveBacking.init" in d.needs for d in rendered)
    assert all(d.needs == () for d in DETECTORS if d.stage in ("request", "static"))
    root = pathlib.Path(webclient.signals.__file__).parent.parent.parent
    r = subprocess.run([sys.executable, str(root / "scripts/gen_docs.py"), "signals", "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_cookie_banner_flag_from_a_consent_platform_or_a_notice_but_not_a_policy_link():
    ctx = lambda body: Context.from_response(200, {"content-type": "text/html"}, {}, body)  # noqa: E731
    ot = flags(ctx(b'<html><head><script src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"></script></head><body></body></html>'))
    assert ot["cookie_banner"].present and ot["cookie_banner"].value == {"vendor": "onetrust"}
    notice = flags(ctx(b'<html><body><div id="cookie-banner">We use cookies <button>OK</button></div></body></html>'))
    assert notice["cookie_banner"].present
    link = flags(ctx(b'<html><body><footer><a class="cookie-policy" href="/cookies">Cookie policy</a></footer></body></html>'))
    assert not link["cookie_banner"].present
