"""One Loop concept (roadmap N9): swappable drivers, manual stepping, Ask checkpoints with
resume, LoopEvents -- across BoundedLoop, the resolve ladder, the crawl, locate, and the
Pipeline type (N12)."""

import pytest

from webclient import Ask, BoundedLoop, LoopEvent, Pipeline, PipelineEvent, Stage, WebClient
from webclient.events import EventBus


def test_bounded_loop_step_run_and_ask_resume():
    bus = EventBus()
    seen = []
    bus.subscribe("loop", seen.append)
    log = []

    def decide(obs):
        if obs == 1:
            return Ask(reason="which way?", options=["left", "right"])
        return f"go-{obs}"

    loop = BoundedLoop(observe=lambda s, i, e: i, decide=decide,
                       done_result=lambda d: "ok" if d == "right" else None,
                       apply=lambda s, d: log.append(d), max_rounds=5, name="t", bus=bus)
    v = loop.run(None)
    assert v.reason == "waiting" and v.ask.reason == "which way?" and loop.pending is v.ask
    assert log == ["go-0"]
    with pytest.raises(RuntimeError):
        BoundedLoop(observe=lambda s, i, e: i, decide=decide, done_result=lambda d: None,
                    apply=lambda s, d: None).resume("x")
    v2 = loop.resume("right")
    assert v2.done and v2.reason == "done" and loop.pending is None
    phases = [e.phase for e in seen if isinstance(e, LoopEvent)]
    assert phases == ["round", "decision", "round", "waiting", "resumed", "round", "decision", "done"]
    # manual: one round at a time with the caller's decision
    m = BoundedLoop(observe=lambda s, i, e: i, decide=lambda o: "auto",
                    done_result=lambda d: "fin" if d == "stop" else None,
                    apply=lambda s, d: log.append(d), max_rounds=3, name="m")
    assert m.step(None, "hand") is None and log[-1] == "hand"
    assert m.step(None) is None and log[-1] == "auto"
    assert m.step(None, "stop").reason == "done" and m.round == 2


def test_resolve_ladder_is_a_loop_with_a_swappable_driver(httpserver):
    from webclient.core.client.resolve_loop import ResolveObservation, default_resolve_driver

    assert default_resolve_driver(ResolveObservation(present=["spa"])) == "browser"
    assert default_resolve_driver(ResolveObservation(present=["login_required"])) == "login"
    assert default_resolve_driver(ResolveObservation(present=["anti_bot_triggered"], antibot_remedy="proxy")) == "proxy"
    assert default_resolve_driver(ResolveObservation(present=["spa"], tiers=["static", "browser"])) is None
    assert default_resolve_driver(ResolveObservation(error="TransportError", bot_block=True)) == "browser"
    assert default_resolve_driver(ResolveObservation(present=[])) is None

    httpserver.expect_request("/spa").respond_with_data(
        '<html><body><div id="root"></div><script src="/a.js"></script></body></html>',
        content_type="text/html")
    seen = []
    with WebClient() as wc:
        wc.bus.subscribe("loop", seen.append)
        decisions = []

        def never_browser(obs):  # a custom driver: log, never escalate
            decisions.append(obs.present)
            return None

        wc.driver("resolve", never_browser)
        assert wc.driver("resolve") is never_browser
        doc = wc.fetch(httpserver.url_for("/spa"), browser="auto")
        assert doc.ok and doc.transport().final_tier == "static" and decisions == [["spa"]]
        assert [e.phase for e in seen if e.loop == "resolve"] == ["round", "decision", "done"]
        wc.driver("resolve", None)
        assert wc.driver("resolve") is None


def test_resolve_driver_can_ask_and_the_caller_escalates_by_hand(httpserver):
    httpserver.expect_request("/spa").respond_with_data(
        '<html><head><title>shell</title></head><body><div id="root"></div>'
        '<script>document.querySelector("#root").innerHTML="<p class=x>rendered</p>"</script></body></html>',
        content_type="text/html")
    with WebClient(timeout=10.0) as wc:
        wc.driver("resolve", lambda obs: Ask(reason="render?", options=["browser"]) if "spa" in obs.present else None)
        doc = wc.fetch(httpserver.url_for("/spa"), browser="auto")
        assert doc.pending is not None and doc.pending.reason == "render?"
        assert doc.transport().final_tier == "static"
        rendered = wc.escalate(doc, "browser")
        assert rendered.pending is None and rendered.name == doc.name
        assert rendered.select(".x").attr("text") == "rendered" and "browser" in rendered.transport().escalation
        wc.release(rendered)


SITE = {
    "/": '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>',
    "/a": '<html><head><title>A</title></head><body><a href="/c">c</a></body></html>',
    "/b": '<html><head><title>B page</title></head><body>b</body></html>',
    "/c": '<html><head><title>C</title></head><body>c</body></html>',
}


@pytest.fixture
def site(httpserver):
    for path, html in SITE.items():
        httpserver.expect_request(path).respond_with_data(html, content_type="text/html")
    return httpserver.url_for


def test_crawl_driver_can_ask_and_resume(site):
    from webclient.loop import Ask as _Ask

    with WebClient() as wc:
        seen = []
        wc.bus.subscribe("loop", seen.append)
        rounds = {"n": 0}

        def driver(crawl):
            rounds["n"] += 1
            if rounds["n"] == 2:
                return _Ask(reason="pick", options=[e.url for e in crawl.frontier])
            return list(crawl.frontier)[:1]

        with wc.crawl(site("/"), driver=driver, browser=False, obey_robots=False, max_pages=10) as crawl:
            crawl.run()
            assert crawl.pending is not None and crawl.pending.reason == "pick"
            before = len(crawl.pages)
            crawl.resume([crawl.pending.options[0]])
            assert crawl.pending is None and len(crawl.pages) > before
            with pytest.raises(RuntimeError):
                crawl.resume([])
        phases = [e.phase for e in seen if e.loop == "crawl"]
        assert "waiting" in phases and "resumed" in phases and phases[0] == "round"


def test_locate_stops_at_the_first_match(site):
    with WebClient() as wc:
        seen = []
        wc.bus.subscribe("loop", seen.append)
        result = wc.locate(site("/"), until=lambda card: (card.title or "").startswith("B"),
                           browser=False, obey_robots=False, max_pages=10, width=1)
        assert result.reason == "found" and [p.title for p in result.found] == ["B page"]
        assert result.pages <= 3 and any(e.loop == "locate" for e in seen)
        none = wc.locate(site("/"), until=lambda card: False, browser=False, obey_robots=False, max_pages=10)
        assert none.reason == "exhausted" and none.found == [] and none.pages == 4


def test_pipeline_runs_gates_reviews_and_checkpoints():
    bus = EventBus()
    seen = []
    bus.subscribe("pipeline", seen.append)
    ctx = {}

    def collect(c):
        return [1, 2, 3]

    def confirm(c):
        return c.get("answer") or Ask(reason="proceed with 3 items?", options=["yes", "no"])

    def author(c):
        return f"query over {len(c['collect'])} items ({c['confirm']})"

    pipe = Pipeline("demo", [
        Stage("collect", run=collect, gate=lambda c, out: bool(out) or "nothing collected",
              review=lambda c, out: {"count": len(out)}),
        Stage("confirm", run=confirm),
        Stage("author", run=author),
    ], bus=bus)
    run = pipe.run(ctx)
    assert run.waiting and run.ask.reason.startswith("proceed") and run.completed == ["collect"]
    assert run.reviews == {"collect": {"count": 3}} and ctx["collect"] == [1, 2, 3]
    run = pipe.resume("yes")
    assert run.ok and run.completed == ["collect", "confirm", "author"]
    assert run.outputs["author"] == "query over 3 items (yes)" and not run.waiting
    phases = [(e.stage, e.phase) for e in seen if isinstance(e, PipelineEvent)]
    assert phases[:4] == [("collect", "enter"), ("collect", "review"), ("collect", "gate"), ("collect", "exit")]
    assert ("confirm", "gate") in phases and phases[-1] == ("author", "exit")
    # a failing gate stops the run (unless optional); a raising stage is recorded
    fail = Pipeline("f", [Stage("a", run=lambda c: 0, gate=lambda c, o: o > 0 or "zero"),
                          Stage("b", run=lambda c: 1)]).run({})
    assert not fail.ok and fail.stopped_at == "a" and fail.reason == "zero" and fail.gates == {"a": "zero"}
    soft = Pipeline("s", [Stage("a", run=lambda c: 0, gate=lambda c, o: o > 0 or "zero", optional=True),
                          Stage("b", run=lambda c: 1)]).run({})
    assert soft.ok and soft.gates == {"a": "zero"}
    boom = Pipeline("x", [Stage("a", run=lambda c: 1 / 0)]).run({})
    assert boom.stopped_at == "a" and "ZeroDivisionError" in boom.reason
