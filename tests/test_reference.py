import pytest

from webclient import Reference


def test_from_url_parses_components():
    ref = Reference.from_url("http://example.com:8080/a/b?x=1&y=2#frag")
    assert ref.scheme == "http"
    assert ref.hostname == "example.com"
    assert ref.port == 8080
    assert ref.path == "/a/b"
    assert ref.params == {"x": "1", "y": "2"}
    assert ref.fragment == "frag"


def test_from_url_defaults():
    ref = Reference.from_url("https://example.com/path")
    assert ref.port is None  # default port for scheme
    assert ref.method == "get"
    assert ref.follow_redirects is True


def test_from_url_preserves_multivalued_params():
    ref = Reference.from_url("https://e.com/?x=1&x=2&y=3")
    assert ref.params == {"x": ["1", "2"], "y": "3"}


def test_from_url_merges_extra_params():
    ref = Reference.from_url("https://e.com/?x=1", params={"y": "2"})
    assert ref.params == {"x": "1", "y": "2"}


def test_url_roundtrip():
    url = "https://example.com/a/b?x=1&y=2#frag"
    assert Reference.from_url(url).url == url


def test_url_elides_default_port_keeps_custom():
    assert Reference(hostname="e.com", scheme="https", port=443).url == "https://e.com"
    assert Reference(hostname="e.com", scheme="http", port=8080).url == "http://e.com:8080"


def test_url_encodes_multivalued_params():
    ref = Reference(hostname="e.com", path="/", params={"x": ["1", "2"]})
    assert ref.url == "https://e.com/?x=1&x=2"


def test_replace_and_with_params_return_copies():
    ref = Reference(hostname="e.com", path="/a", params={"x": "1"})
    other = ref.replace(path="/b")
    merged = ref.with_params(y="2")
    assert other.path == "/b" and ref.path == "/a"
    assert merged.params == {"x": "1", "y": "2"} and ref.params == {"x": "1"}


def test_join_resolves_relative_and_absolute():
    ref = Reference.from_url("https://example.com/list")
    assert ref.join("items/3").url == "https://example.com/items/3"
    assert ref.join("/items/1?ref=home").params == {"ref": "home"}
    assert ref.join("https://other.example/x").hostname == "other.example"


def test_bind_returns_bound_copy():
    ref = Reference(hostname="e.com")
    client = object()
    bound = ref.bind(client)
    assert bound._client is client
    assert ref._client is None


def test_bound_reference_carries_client():
    from webclient import WebClient
    with WebClient() as wc:
        ref = wc.ref("https://example.com/x")          # a lazy reference root
        assert ref._client is wc.core                  # bound to this client's core
