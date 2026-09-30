"""Evaluate -- the DETERMINISTIC dataset-likeness read of a page, and the EVALUATE stage that has
the model judge one candidate as the source to scrape.

Two layers, deliberately:

* the deterministic helpers (:func:`score` / :func:`present` / the flags-derived facts) are the
  ground truth about a page's STRUCTURE -- a login wall, an anti-bot block, API docs, a repeating
  record region, a data-API. They cost nothing, they gate hard (a wall never outranks a clean
  source however table-like), and they are what every LLM step is told, never what it guesses.
* :func:`evaluate_candidate` is the ONE stage that shows the model a page skeleton -- clipped to
  a hard budget -- plus those flags and the observed data endpoints, and asks for a structured
  JSON verdict (present? queryable? which endpoint? sort order + where the newest records are?
  paginated? scrapability?). It runs as a short CONVERSATION: the skeleton is the opening turn,
  and an unparseable reply costs only a short "reply with ONLY valid JSON" follow-up, never a
  re-send of the page. It FAILS OPEN to the deterministic read (a model outage is a retry, not
  "no data here"). :func:`evaluate_candidates` runs it best-tier-first and stops early on the
  ideal case (a ``must`` that is itself a queryable dataset).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from urllib.parse import urlparse

from web.fetch import WebException, emit
from web.parse import Document
from web.resolve import Flag, flags

from .llm import Conversational, Llm, ReasonEvent, parse_json
from .models import DOWNLOAD_EXTENSIONS, Candidate, CandidateEval, LocateBrief, Reference
from .patterns import brief_hints
from .prompts import MAX_SKELETON_CHARS, clip, render_prompt

#: URL markers of API DOCUMENTATION / dev portals -- never a scrapable dataset, even if crawled.
_DOCS = (
    "/docs",
    "/documentation",
    "/developer",
    "/developers",
    "/api-docs",
    "/swagger",
    "/redoc",
    "/reference/",
    "/help/",
    "/support/",
)
#: pager remedy strings, in the order they win (a real pager beats scroll beats a cursor guess).
_PAGERS = (("paginated", "paginate"), ("infinite_scroll", "paginate:scroll"))
#: the select tiers, best first -- evaluation order.
TIER_RANK = {"must": 0, "should": 1, "could": 2}
#: the follow-up turn when the model's verdict was not JSON -- short, so the skeleton is never
#: re-sent (it stays in the conversation's cached opening).
_JSON_RETRY = (
    "Your previous reply could not be parsed as JSON. Reply again with ONLY the JSON object "
    "described above -- no prose, no code fences."
)


# -- deterministic reads: the ground truth every LLM step is told ---------------------------------


def is_docs(url: str) -> bool:
    """API / product DOCUMENTATION by URL -- never the data source."""
    low = url.lower()
    return any(m in low for m in _DOCS)


def present(doc: Document, by: "dict[str, Flag]") -> bool:
    """Whether the dataset is plausibly ON this page: a JSON doc IS data, else a repeating record
    region / structured data / a data island must be present."""
    if doc.kind == "json":
        return True
    return bool(doc.records(top_k=1)) or "structured_data" in by or "data_api" in by


def score(doc: Document, by: "dict[str, Flag]") -> float:
    """A deterministic dataset-likeness score: dataset-present x scrapability, minus gates that
    block extraction. Higher is a better source. A hard GATE (a login wall, API docs, or an anti-bot
    block) disqualifies the page outright -- its "records" are the wall/challenge, not the dataset,
    so it must never outrank a clean source no matter how table-like it looks."""
    if "auth_required" in by:  # a login wall -- no query reaches the dataset
        return -1.0
    # any tier of anti-bot wall (ANTI-BOT.md §4) is the challenge/block page, not the dataset.
    if any(f in by for f in ("js_challenge", "captcha", "ip_blocked", "rate_limited")):
        return -1.0
    if is_docs(doc.url):  # API docs are never the data source
        return -1.0
    if not present(doc, by):
        return 0.0
    if doc.kind == "json":  # a served JSON/feed document is the dataset itself
        return 9.0
    regions = doc.records(top_k=1)
    # cap the record-region contribution so a huge nav/boilerplate table cannot dominate a real
    # (smaller) dataset -- record PRESENCE matters more than raw region size.
    base = 4.0 + (min(regions[0].score, 40.0) * 0.1 if regions else 0.0)
    if "structured_data" in by:
        base += 2.0  # machine-readable data about itself -- cleaner to extract
    if "data_api" in by:
        base += 1.0
    if "empty" in by:
        base -= 3.0
    return base


def field_bonus(doc: Document, fields: "list[str]") -> float:
    """A small score boost when the page actually SHOWS the fields the brief's schema asks for --
    so the RIGHT dataset is recognised among several record lists (the schema is SHARED: Author
    extracts these fields, Locate uses them to find them). A tiebreaker, capped so record-presence
    still dominates; matched as whole words in the page/JSON text."""
    if not fields:
        return 0.0
    tokens = set(re.findall(r"[a-z0-9]+", doc.text.lower()))
    hits = sum(1 for f in fields if all(t in tokens for t in re.findall(r"[a-z0-9]+", f.lower())))
    return min(2.0, hits * 0.5)


def download_targets(doc: Document) -> "list[str]":
    """The downloadable-file links on the page (by extension) -- the 'records' of a download brief."""
    return [u for u in doc.links() if u.lower().split("?")[0].endswith(DOWNLOAD_EXTENSIONS)]


def ignored(url: str, ignore: "list[str]") -> bool:
    """Whether the URL's HOST matches any brief ``ignore`` entry -- a HARD exclusion of the sources
    the brief forbids (third-party aggregators), so one never wins even as the only survivor when
    the real source 404s (Locate then FAILS, which is right). Matches domain-ish ignore tokens
    (``benzinga`` -> benzinga.com) as host substrings; a purely descriptive entry doesn't match --
    best-effort, the model's judgement applies on top. Host-only, never the path."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for entry in ignore:
        for token in re.findall(r"[a-z0-9]+", entry.lower()):
            if len(token) >= 4 and token in host:
                return True
    return False


def record_selector(doc: Document) -> "str | None":
    regions = doc.records(top_k=1)
    return regions[0].item_selector if regions else None


def pagination(by: "dict[str, Flag]") -> "str | None":
    for name, remedy in _PAGERS:
        if name in by:
            return remedy
    return None


def reason(doc: Document, by: "dict[str, Flag]") -> str:
    """A human WHY this page looks like the dataset."""
    bits: list[str] = []
    if doc.kind == "json":
        bits.append("a JSON data document (the dataset itself)")
    elif "record_list" in by:
        bits.append(f"a repeating record region ({record_selector(doc)})")
    if "structured_data" in by:
        bits.append("machine-readable structured data (JSON-LD/microdata)")
    if "data_api" in by:
        bits.append("a JSON data-API backs the page")
    if "paginated" in by:
        bits.append("paginated (the pipeline follows the pager)")
    return "; ".join(bits) or "the highest dataset-likeness score among the candidates"


def reference(doc: Document, by: "dict[str, Flag]") -> Reference:
    """The Reference for a chosen page from its flags + record detection (pre-API, pre-profile:
    the LOAD stage stamps the transport that actually loads the data)."""
    render = {"needs_browser", "spa", "iframe"}
    return Reference(
        url=doc.url,
        kind=doc.kind,
        page_url=doc.url,
        flags=sorted(by),
        signals=sorted({s.name for f in by.values() for s in f.signals}),
        assessment=sorted(by.values(), key=lambda f: -f.confidence),  # why each fired
        record_selector=record_selector(doc),
        pagination=pagination(by),
        needs_browser=any(n in by for n in render),
        detail={"score": round(score(doc, by), 3), "reason": reason(doc, by)},
    )


def data_api_endpoints(doc: Document) -> list[str]:
    """Same-origin JSON/data-API URLs the page points at: declared JSON/feed ``<link>``s, then any
    ``/api/``- or ``.json``-looking link. Deterministic (the DOM only), most-declared first,
    deduped -- the "observed data endpoints" the evaluate prompt may name (never invent one)."""
    host = urlparse(doc.url).hostname
    out: list[str] = []
    declared = list(doc.metadata().feeds)
    for el in doc.select_all("link[rel=alternate][type*=json], link[type*=json]"):
        href = el.attr("href")
        if href:
            declared.append(href)
    linky = [
        u for u in doc.links() if "/api/" in u.lower() or u.lower().split("?")[0].endswith(".json")
    ]
    for u in [*declared, *linky]:
        if urlparse(u).hostname == host and u not in out:
            out.append(u)
    return out


def skeleton_for(doc: Document) -> str:
    """The page skeleton to hand the model, CLIPPED to budget: the JSON shape for a JSON document,
    else the record-marked DOM outline. A big page drops its nav/footer chrome first so the record
    region isn't clipped away under menus; a small page keeps the faithful view."""
    if doc.kind == "json":
        return clip(doc.json_skeleton(max_lines=4000), MAX_SKELETON_CHARS, "skeleton", kind="json")
    skel = doc.skeleton(max_lines=4000)
    if len(skel) > MAX_SKELETON_CHARS:
        skel = doc.skeleton(max_lines=4000, drop_chrome=True)
    return clip(skel, MAX_SKELETON_CHARS, "skeleton", kind="html")


# -- the evaluate stage ---------------------------------------------------------------------------


def _as_bool(v: object) -> bool:
    """A model's truthy/falsey field, treating the STRING tokens a cheap model emits ("false" /
    "no" / "0") as False -- so ``"dataset_present": "false"`` isn't read as truthy."""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "", "none", "null")
    return bool(v)


def _as_int(v: object, lo: int = 0, hi: int = 10) -> int:
    if isinstance(v, bool):
        return lo
    if isinstance(v, (int, float)):
        return max(lo, min(hi, int(v)))
    if isinstance(v, str):
        try:
            return max(lo, min(hi, int(float(v))))
        except ValueError:
            return lo
    return lo


def _as_str(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def _flag_report(by: "dict[str, Flag]") -> "tuple[dict[str, float], dict[str, list[str]]]":
    """The present flags (name -> confidence) and, per flag, the readable evidence behind it."""
    flag_map = {n: round(f.confidence, 2) for n, f in by.items() if f.present}
    flag_signals = {
        n: [
            f"{s.name} ({s.confidence:.2f})" + (f": {s.detail}" if s.detail else "")
            for s in f.signals
        ]
        for n, f in by.items()
        if f.present
    }
    return flag_map, flag_signals


def deterministic_eval(
    doc: Document,
    by: "dict[str, Flag]",
    *,
    llm_unavailable: bool = False,
    note: str = "",
) -> CandidateEval:
    """The verdict from the page's OWN signals, no model: present iff a dataset structure is there
    and no gate blocks it; scrapability from the score. The fallback when no model is configured
    or the model could not answer (then ``llm_unavailable`` says so -- a retry, not a dead source).
    """
    flag_map, flag_signals = _flag_report(by)
    s = score(doc, by)
    ok = s > 0.0 and present(doc, by)
    why = ("deterministic: " + reason(doc, by)) if ok else "deterministic: no dataset detected"
    return CandidateEval(
        url=doc.url,
        dataset_present=ok,
        is_queryable=ok and doc.kind == "json",
        has_pagination=any(n in by for n, _ in _PAGERS),
        pagination=pagination(by),
        scrapability=max(0, min(10, round(s))) if ok else 0,
        verdict=why + (f" ({note})" if note else ""),
        flags=flag_map,
        flag_signals=flag_signals,
        llm_unavailable=llm_unavailable,
    )


async def evaluate_candidate(
    cand: Candidate,
    doc: Document,
    brief: LocateBrief,
    *,
    llm: "Llm | None",
    entity: str = "",
) -> CandidateEval:
    """Have the model judge ONE candidate from its clipped skeleton + flags + observed endpoints ->
    a :class:`CandidateEval`. The flags are ground truth for structure, so they WIN over the model's
    guesses (pagination, the endpoint -- which must be one actually observed, never invented -- and
    API docs, which are never a source). A binary document is the deliverable (a download, no
    model call); a login wall drops the candidate. Runs as a conversation: skeleton in the opening,
    one short JSON-retry follow-up at most; a model error or a still-unparseable reply FAILS OPEN
    to the deterministic read."""
    by = {f.name: f for f in flags(doc)}
    flag_map, flag_signals = _flag_report(by)
    if "auth_required" in by:  # a login wall blocks the dataset -- no query reaches it
        return CandidateEval(
            url=doc.url, verdict="login required", flags=flag_map, flag_signals=flag_signals
        )
    if doc.kind not in ("html", "xml", "json"):  # a PDF / spreadsheet / blob IS the deliverable
        return CandidateEval(
            url=doc.url,
            dataset_present=True,
            is_queryable=False,
            completeness="full",
            scrapability=6,
            verdict=f"a {doc.kind} document (a download)",
            flags=flag_map,
            flag_signals=flag_signals,
        )
    if llm is None:
        return deterministic_eval(doc, by)
    endpoints = data_api_endpoints(doc)
    exit_condition = (
        f"EXIT CONDITION (from the brief): {brief.exit_when} If this holds for THIS page, set "
        "exit_when_met=true with a one-line exit_reason; the pipeline will then stop cleanly "
        "WITHOUT authoring a query.\n"
        if brief.exit_when
        else ""
    )
    scope = f" It must be {entity}'s OWN data, not a third party's page ABOUT it." if entity else ""
    prompt = render_prompt(
        "evaluate_candidate",
        description=(brief.goal or "the target dataset") + scope,
        fields_line=brief_hints(brief),
        candidate_url=doc.url,
        flag_map_json=json.dumps(flag_map),
        endpoints_json=json.dumps(endpoints),
        skeleton=skeleton_for(doc),
        exit_condition=exit_condition,
    )
    conv = llm.conversation() if isinstance(llm, Conversational) else None
    try:
        reply = await (conv.send(prompt) if conv is not None else llm.complete(prompt))
        data = parse_json(reply)
        if not isinstance(data, dict):  # one short retry turn -- the skeleton is NOT re-sent
            retry = _JSON_RETRY if conv is not None else f"{prompt}\n\n---\n{_JSON_RETRY}"
            reply = await (conv.send(retry) if conv is not None else llm.complete(retry))
            data = parse_json(reply)
    except WebException as exc:  # the MODEL was unavailable -- judge deterministically, say so
        emit(
            ReasonEvent(
                stage="evaluate", subject=doc.url, text=f"model error, judged by signals: {exc}"
            )
        )
        return deterministic_eval(doc, by, llm_unavailable=True, note="model unavailable")
    if not isinstance(data, dict):
        emit(
            ReasonEvent(
                stage="evaluate", subject=doc.url, text="verdict was not JSON — judged by signals"
            )
        )
        return deterministic_eval(doc, by, note="unparseable verdict")
    # the flags are ground truth for structure -> they win over the model's guesses.
    llm_ep = _as_str(data.get("api_endpoint"))
    api_endpoint = llm_ep if llm_ep in set(endpoints) else None  # only an OBSERVED endpoint
    is_api_docs = _as_bool(data.get("is_api_docs")) or is_docs(doc.url)
    dataset_present = _as_bool(data.get("dataset_present")) and not is_api_docs
    ev = CandidateEval(
        url=doc.url,
        dataset_present=dataset_present,
        is_queryable=(_as_bool(data.get("is_queryable")) and not is_api_docs),
        api_endpoint=api_endpoint,
        sort_order=_as_str(data.get("sort_order")) or None,
        recency_hint=_as_str(data.get("recency_hint")),
        completeness=_as_str(data.get("completeness")) or None,
        has_pagination=_as_bool(data.get("has_pagination")) or any(n in by for n, _ in _PAGERS),
        pagination=pagination(by),
        has_filters=_as_bool(data.get("has_filters")),
        dataset_is_subset=_as_bool(data.get("dataset_is_subset")),
        mostly_unstructured=_as_bool(data.get("mostly_unstructured")),
        drilldown_links=_as_bool(data.get("drilldown_links")),
        is_api_docs=is_api_docs,
        interactive=("consent_wall" in by),
        scrapability=_as_int(data.get("scrapability")),
        verdict=_as_str(data.get("verdict")) or "(no reason given)",
        flags=flag_map,
        flag_signals=flag_signals,
        exit_when_met=bool(brief.exit_when) and _as_bool(data.get("exit_when_met")),
        exit_reason=_as_str(data.get("exit_reason")) if brief.exit_when else "",
    )
    return ev


def candidate_score(ev: CandidateEval, tier: str = "") -> float:
    """Rank an evaluated candidate. The select TIER (the model's judgement of which page IS the
    dataset) is the PRIMARY signal, so a per-record drill-down endpoint that merely happens to be
    queryable can't outrank the listing marked ``must``. Within a tier: queryable, then
    scrapability. A source without the dataset is never preferred over one that has it."""
    if not ev.dataset_present:
        return -1.0
    tier_rank = {"must": 2, "should": 1}.get(tier, 0)
    return tier_rank * 100 + (4 if ev.is_queryable else 0) + ev.scrapability


async def evaluate_candidates(
    candidates: "Sequence[Candidate]",
    docs: "dict[str, Document]",
    brief: LocateBrief,
    *,
    llm: "Llm | None",
    entity: str = "",
) -> "tuple[CandidateEval, Candidate] | None":
    """Evaluate best-tier-first until a usable source is found -- EARLY EXIT only on the IDEAL case
    (a ``must`` that is itself a queryable dataset; a lower-tier queryable candidate may be a
    drill-down and must not preempt) -- else the best-scoring evaluation seen. ``None`` when
    nothing held the dataset. Returns the evaluation with the candidate it came from."""
    best: "tuple[CandidateEval, Candidate] | None" = None
    for cand in sorted(candidates, key=lambda c: TIER_RANK.get(c.tier, 3)):
        doc = docs.get(cand.url)
        if doc is None:
            continue
        ev = await evaluate_candidate(cand, doc, brief, llm=llm, entity=entity)
        emit(
            ReasonEvent(
                stage="evaluate",
                subject=cand.url,
                text=(
                    f"[{cand.tier}] present={ev.dataset_present} queryable={ev.is_queryable} "
                    f"scrapability={ev.scrapability}"
                    + (" [API DOCS]" if ev.is_api_docs else "")
                    + f" — {ev.verdict}"
                ),
            )
        )
        if best is None or candidate_score(ev, cand.tier) > candidate_score(best[0], best[1].tier):
            best = (ev, cand)
        if ev.usable and ev.is_queryable and cand.tier == "must":
            return ev, cand
    if best is None or not best[0].dataset_present:
        return None
    return best


__all__ = [
    "TIER_RANK",
    "is_docs",
    "present",
    "score",
    "field_bonus",
    "download_targets",
    "ignored",
    "record_selector",
    "pagination",
    "reason",
    "reference",
    "data_api_endpoints",
    "skeleton_for",
    "deterministic_eval",
    "evaluate_candidate",
    "candidate_score",
    "evaluate_candidates",
]
