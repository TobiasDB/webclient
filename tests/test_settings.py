"""Settings: one pydantic object for the useful config, env-loadable + buildable."""

from webclient import BrowserConfig, LlmSettings, Settings, WebClient


def test_defaults_and_composition():
    s = Settings()
    assert s.timeout == 30.0 and s.retries == 0
    assert isinstance(s.browser, BrowserConfig) and s.browser.stealth  # stealth default
    assert isinstance(s.llm, LlmSettings) and s.llm.budget_usd is None


def test_from_env_reads_the_prefixed_vars():
    s = Settings.from_env(env={
        "WEBCLIENT_TIMEOUT": "12.5",
        "WEBCLIENT_RETRIES": "3",
        "WEBCLIENT_BLOCK_PRIVATE_HOSTS": "true",
        "WEBCLIENT_BROWSER_HEADLESS": "false",
        "WEBCLIENT_BROWSER_FINGERPRINT": "on",
        "WEBCLIENT_LLM_MODEL": "claude-sonnet-5",
        "WEBCLIENT_LLM_BUDGET_USD": "2.50",
    })
    assert s.timeout == 12.5 and s.retries == 3 and s.block_private_hosts
    assert s.browser.headless is False and s.browser.fingerprint and s.browser.stealth
    assert s.llm.model == "claude-sonnet-5" and s.llm.budget_usd == 2.50


def test_builds_a_configured_client():
    s = Settings.from_env(env={"WEBCLIENT_TIMEOUT": "7", "WEBCLIENT_BROWSER_HEADLESS": "false"})
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
