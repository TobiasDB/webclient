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
