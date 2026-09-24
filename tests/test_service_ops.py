"""``GET /ops``: the op catalogue a UI builds an object's menu from -- generated from the cores'
backing tables (the same source as the typed surface), never hand-listed."""

from webclient.service import op_catalogue


def test_op_catalogue_lists_document_ops_with_params():
    cat = op_catalogue()
    doc = {o["name"]: o for o in cat["Document"]}
    assert doc["select"]["params"][0] == {"name": "selector", "required": True, "kind": "positional", "default": None, "type": "str"}
    assert doc["write"]["io"] and doc["select_all"]["collection"]
    assert doc["attr"]["params"][0]["name"] == "name"
    assert doc["title"]["kind"] == "prop"
    # the hand-written chain ops ride along, with paginate's real signature
    assert doc["extract"]["bound"] and {p["name"] for p in doc["paginate"]["params"]} >= {"by", "max_pages", "next", "records"}
    assert {o["name"] for o in cat["Reference"]} >= {"resolve", "with_params"}


def test_ops_endpoint():
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    with TestClient(create_app()) as client:
        r = client.get("/ops")
        assert r.status_code == 200
        names = {o["name"] for o in r.json()["Document"]}
        assert {"select", "select_all", "attr", "click", "paginate", "markdown"} <= names
