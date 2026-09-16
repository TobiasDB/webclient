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
