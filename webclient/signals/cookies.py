"""Cookie / consent banners: the ``cookie_banner`` flag and the page script that dismisses them.

A consent banner sits over the content a scrape wants (and on some sites blocks scrolling or
clicks until it is answered). Detection reads the consent PLATFORM from the served HTML (its
loader script / its container) or a generic cookie notice; a browser render runs
:data:`DISMISS_JS` -- ``wc.cookies``, an ``inline`` script, so before the HTML snapshot -- which
answers the banner (REJECT / "necessary only" first, else accept), hides it if nothing answers,
restores page scrolling, and reports what it did; that report is the rendered evidence.

    wc.scripts.disable("wc.cookies")   # keep banners (e.g. to scrape the banner itself)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .context import Context
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Signal

#: consent platforms: name -> (loader-script / markup markers in the served HTML)
PLATFORMS: dict[str, tuple[str, ...]] = {
    "onetrust": ("cdn.cookielaw.org", "optanon", "onetrust-banner-sdk", "onetrust-consent-sdk"),
    "cookiebot": ("consent.cookiebot.com", "cybotcookiebotdialog"),
    "didomi": ("sdk.privacy-center.org", "didomi-host", "didomi-notice"),
    "quantcast": ("quantcast.mgr.consensu.org", "cmp.quantcast.com", "qc-cmp2"),
    "usercentrics": ("app.usercentrics.eu", "usercentrics-root", "web.cmp.usercentrics.eu"),
    "trustarc": ("consent.trustarc.com", "truste-consent"),
    "sourcepoint": ("sourcepoint.mgr.consensu.org", "sp_message_container", "cdn.privacy-mgmt.com"),
    "funding-choices": ("fundingchoicesmessages.google.com", "fc-consent-root"),
    "osano": ("cmp.osano.com", "osano-cm-window"),
    "iubenda": ("cdn.iubenda.com/cs", "iubenda-cs-banner"),
    "complianz": ("cmplz-cookiebanner",),
    "cookieyes": ("cdn-cookieyes.com", "cky-consent-container", "cookie-law-info-bar"),
    "cookieconsent": ("cookieconsent.min.js", "cc-window", "cookieconsent"),
    "termly": ("app.termly.io",),
    "cookie-script": ("cdn.cookie-script.com", "cookiescript_injected"),
}

#: a generic notice: an element whose id/class names a cookie / consent banner
_GENERIC_RE = re.compile(r'(?:id|class)=["\'][^"\']*(?:cookie[-_ ]?(?:banner|notice|consent|bar|popup|wall|modal|dialog)|consent[-_ ]?(?:banner|popup|modal|bar)|gdpr[-_ ]?(?:banner|notice|popup)|cmp[-_ ]?(?:banner|container))', re.I)


def _platform_value(signals: "list[Signal]", ctx: Context) -> Any:
    """The flag's value: what the page script did (rendered), else the platform found (static)."""
    got = (ctx.render_stats or {}).get("cookie_banner")
    if isinstance(got, dict) and got.get("found"):
        return got
    for s in signals:
        if s.value:
            return {"vendor": s.value}
    return None


# the remedy: a browser render (its wc.cookies page script answers the banner before the snapshot)
flag("cookie_banner", value=_platform_value, remedy="browser")


@detector(flag="cookie_banner", name="consent_platform", stage="static")
def _consent_platform(ctx: Context) -> Hit | None:
    """cookie_banner evidence (strong): a consent-management platform's loader or container in
    the served HTML (OneTrust, Cookiebot, Didomi, Quantcast, Usercentrics, TrustArc, …)."""
    low = ctx.low
    for name, marks in PLATFORMS.items():
        if any(m in low for m in marks):
            return Hit(0.85, f"a {name} consent banner", name)
    return None


@detector(flag="cookie_banner", name="cookie_notice", stage="static")
def _cookie_notice(ctx: Context) -> Hit | None:
    """cookie_banner evidence: an element named as a cookie / consent / GDPR banner."""
    if _GENERIC_RE.search(ctx.text or ""):
        return Hit(0.6, "a cookie / consent notice element")
    return None


@detector(flag="cookie_banner", name="banner_answered", stage="rendered", needs=("LiveBacking.init", "wc.cookies"))
def _banner_answered(ctx: Context) -> Hit | None:
    """cookie_banner evidence (rendered): the page script found a visible banner and answered /
    hid it before the snapshot."""
    got = (ctx.render_stats or {}).get("cookie_banner")
    if isinstance(got, dict) and got.get("found"):
        return Hit(0.95, f"a {got.get('vendor') or 'cookie'} banner, {got.get('action')} before the snapshot", got)
    return None


#: the ``wc.cookies`` inline page script: find a VISIBLE consent banner (a known platform's
#: container, else a fixed / dialog element that talks about cookies), answer it -- reject /
#: necessary-only first, else accept -- hide it when nothing answers, restore scrolling, and
#: return ``{cookie_banner: {found, vendor, action, button, text}}`` (merged into dom_stats).
DISMISS_JS = r"""async () => {
  const VENDORS = [
    ["onetrust", "#onetrust-banner-sdk, #onetrust-consent-sdk", ["#onetrust-reject-all-handler", ".ot-pc-refuse-all-handler"], ["#onetrust-accept-btn-handler"]],
    ["cookiebot", "#CybotCookiebotDialog", ["#CybotCookiebotDialogBodyButtonDecline", "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll"], ["#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll", "#CybotCookiebotDialogBodyButtonAccept"]],
    ["didomi", "#didomi-notice, #didomi-host", ["#didomi-notice-disagree-button", ".didomi-continue-without-agreeing"], ["#didomi-notice-agree-button"]],
    ["quantcast", "#qc-cmp2-ui, .qc-cmp2-container", [".qc-cmp2-summary-buttons button[mode=secondary]"], [".qc-cmp2-summary-buttons button[mode=primary]"]],
    ["trustarc", "#truste-consent-track, #truste-consent-content", ["#truste-consent-required"], ["#truste-consent-button"]],
    ["funding-choices", ".fc-consent-root", [".fc-cta-do-not-consent"], [".fc-cta-consent"]],
    ["osano", ".osano-cm-window", [".osano-cm-denyAll", ".osano-cm-deny"], [".osano-cm-accept-all", ".osano-cm-accept"]],
    ["iubenda", "#iubenda-cs-banner", [".iubenda-cs-reject-btn"], [".iubenda-cs-accept-btn"]],
    ["complianz", "#cmplz-cookiebanner-container, .cmplz-cookiebanner", [".cmplz-deny"], [".cmplz-accept"]],
    ["cookieyes", ".cky-consent-container, #cookie-law-info-bar", [".cky-btn-reject", "#cookie_action_close_header_reject"], [".cky-btn-accept", "#cookie_action_close_header"]],
    ["cookieconsent", ".cc-window, #cc-main .cm", [".cc-deny", "[data-role=necessary]", ".cc-dismiss"], [".cc-allow", "[data-role=all]"]],
    ["usercentrics", "#usercentrics-root", [], []],
    ["sourcepoint", "[id^=sp_message_container]", [], []],
  ];
  const REJECT = /\b(reject|decline|deny|refuse|disagree|ablehnen|refuser|rechazar|rifiuta)\b|(only|just)\s+(necessary|essential|required)|(necessary|essential|required)\s+(cookies\s+)?only|continue\s+without/i;
  const ACCEPT = /\b(accept|agree|allow|got it|ok|okay|i understand|understood|consent|akzeptieren|zustimmen|accepter|aceptar|accetta)\b/i;
  const TALKS = /cookie|consent|gdpr|privacy|tracking|personali[sz]/i;
  const visible = (el) => { if (!el || !el.getBoundingClientRect) return false; const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false; const cs = getComputedStyle(el); return cs.display !== "none" && cs.visibility !== "hidden" && Number(cs.opacity) > 0.05; };
  const text = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const roots = (el) => { const out = [el]; if (el.shadowRoot) out.push(el.shadowRoot); for (const x of el.querySelectorAll("*")) if (x.shadowRoot) out.push(x.shadowRoot); return out; };
  const buttonsIn = (el) => roots(el).flatMap((r) => [...r.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]")]).filter(visible);
  const pick = (el, sels, re) => { for (const s of sels) { for (const r of roots(el)) { const b = r.querySelector(s); if (b && visible(b)) return b; } } if (!re) return null; return buttonsIn(el).find((b) => re.test(text(b) || b.value || b.getAttribute("aria-label") || "")) || null; };
  // 1. a known platform's banner, else 2. a generic one: fixed / sticky / dialog, talking about cookies
  let found = null, vendor = null, rej = [], acc = [];
  for (const [name, sel, r, a] of VENDORS) { const el = document.querySelector(sel); if (el && (visible(el) || (el.shadowRoot && text(el.shadowRoot.host || el).length))) { found = el; vendor = name; rej = r; acc = a; break; } }
  if (!found) {
    const cands = [...document.querySelectorAll("div, section, aside, footer, form, dialog, [role=dialog], [aria-modal=true]")].filter((el) => {
      const cs = getComputedStyle(el); const pos = cs.position === "fixed" || cs.position === "sticky" || el.getAttribute("role") === "dialog" || el.getAttribute("aria-modal") === "true" || el.tagName === "DIALOG";
      const named = /cookie|consent|gdpr|cmp/i.test((el.id || "") + " " + (typeof el.className === "string" ? el.className : ""));
      if (!(pos || named) || !visible(el)) return false; const t = text(el); return t.length > 20 && t.length < 3000 && TALKS.test(t);
    });
    found = cands.sort((x, y) => text(x).length - text(y).length)[0] || null;
  }
  if (!found) return {};
  const out = { found: true, vendor, action: "none", button: null, text: text(found).slice(0, 100) };
  try {
    const b = pick(found, rej, REJECT) || pick(found, acc, ACCEPT);
    if (b) { out.button = text(b).slice(0, 40) || b.id || b.className; out.action = REJECT.test(text(b)) || rej.some((s) => b.matches && b.matches(s)) ? "rejected" : "accepted"; b.click(); await new Promise((r) => setTimeout(r, 500)); }
  } catch (e) { out.error = String(e).slice(0, 80); }
  // still there: hide it (and any backdrop it put over the page)
  if (visible(found)) { found.style.setProperty("display", "none", "important"); out.action = out.action === "none" ? "hidden" : out.action + "+hidden"; }
  for (const el of [document.documentElement, document.body]) { if (el && getComputedStyle(el).overflow === "hidden") el.style.setProperty("overflow", "auto", "important"); }
  return { cookie_banner: out };
}"""

__all__ = ["DISMISS_JS", "PLATFORMS"]
