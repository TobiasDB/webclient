"""Settings: one pydantic object for the useful config, env-loadable + buildable."""

from webclient import BrowserConfig, LlmSettings, Settings, WebClient


def test_defaults_and_composition():
    s = Settings()
    assert s.timeout == 30.0 and s.retries == 0
    assert isinstance(s.browser, BrowserConfig) and s.browser.stealth  # stealth default
    assert isinstance(s.llm, LlmSettings) and s.llm.budget_usd is None


def test_from_env_reads_the_prefixed_vars(monkeypatch):
    for k, v in {
        "WEBCLIENT_TIMEOUT": "12.5",
        "WEBCLIENT_RETRIES": "3",
        "WEBCLIENT_BLOCK_PRIVATE_HOSTS": "true",
        "WEBCLIENT_BROWSER__HEADLESS": "false",  # nested via the __ delimiter
        "WEBCLIENT_BROWSER__FINGERPRINT": "on",
        "WEBCLIENT_LLM__MODEL": "claude-sonnet-5",
        "WEBCLIENT_LLM__BUDGET_USD": "2.50",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings()  # reads the environment
    assert s.timeout == 12.5 and s.retries == 3 and s.block_private_hosts
    assert s.browser.headless is False and s.browser.fingerprint and s.browser.stealth
    assert s.llm.model == "claude-sonnet-5" and s.llm.budget_usd == 2.50


def test_builds_a_configured_client(monkeypatch):
    monkeypatch.setenv("WEBCLIENT_TIMEOUT", "7")
    monkeypatch.setenv("WEBCLIENT_BROWSER__HEADLESS", "false")
    s = Settings()
    with s.client() as wc:
        assert isinstance(wc, WebClient)
        assert wc.timeout == 7.0
        assert wc.browser_config.headless is False and wc.browser_config.stealth
    # per-call overrides win
    with s.client(timeout=99) as wc2:
        assert wc2.timeout == 99.0


def test_builds_a_configured_llm_client():
    s = Settings(llm=LlmSettings(model="claude-haiku-4-5", budget_usd=1.0))
    llm = s.llm_client(auth="test-key")
    assert llm.model == "claude-haiku-4-5" and llm.auth == "test-key"
    assert llm.budget.max_usd == 1.0
    llm.close()


def test_tuning_groups_default_and_read_from_env(monkeypatch):
    s = Settings()
    assert s.limits.chatty_round_trips == 4 and s.detection.present_threshold == 0.5
    assert s.service.max_docs == 1024 and s.loops.max_rounds == 20
    monkeypatch.setenv("WEBCLIENT_LIMITS__CHATTY_ROUND_TRIPS", "9")
    monkeypatch.setenv("WEBCLIENT_DETECTION__PRESENT_THRESHOLD", "0.8")
    monkeypatch.setenv("WEBCLIENT_LOOPS__MAX_ROUNDS", "2")
    monkeypatch.setenv("WEBCLIENT_LOG_LEVEL", "debug")
    s = Settings()
    assert s.limits.chatty_round_trips == 9 and s.detection.present_threshold == 0.8
    assert s.loops.max_rounds == 2 and s.log_level == "debug"


def test_current_settings_are_read_at_use_time():
    from webclient import use_settings, current_settings
    from webclient.loop import BoundedLoop
    from webclient.settings import LoopSettings

    previous = use_settings(Settings(loops=LoopSettings(max_rounds=2, max_stalls=1)))
    try:
        assert current_settings().loops.max_rounds == 2
        loop = BoundedLoop(observe=lambda s, i, e: i, decide=lambda o: o,
                           done_result=lambda d: None, apply=lambda s, d: None)
        assert loop.max_rounds == 2 and loop.max_stalls == 1
        assert loop.run(None).reason == "budget"
    finally:
        use_settings(previous)
    # an explicit budget still wins over the settings
    loop = BoundedLoop(observe=lambda s, i, e: i, decide=lambda o: o,
                       done_result=lambda d: None, apply=lambda s, d: None, max_rounds=1)
    assert loop.max_rounds == 1


def test_detection_threshold_is_a_setting():
    from webclient import use_settings
    from webclient.core.document.models import Signal
    from webclient.settings import DetectionSettings
    from webclient.signals import build_flag

    sig = [Signal(name="x", flag="f", stage="static", confidence=0.6)]
    assert build_flag("f", sig).present
    previous = use_settings(Settings(detection=DetectionSettings(present_threshold=0.9)))
    try:
        assert not build_flag("f", sig).present
    finally:
        use_settings(previous)
