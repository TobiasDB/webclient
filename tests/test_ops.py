"""Dispatch model: a WebCore chooses the backings that apply to its state and
dispatches an op to the first that provides it; missing ops / absent
capabilities raise UnsupportedOp. (Replaces the pre-rewrite @policy op tests.)"""
import pytest
from typing import ClassVar

from pydantic import BaseModel

from webclient.core.web_core import Backing, UnsupportedOp, WebCore


class Always(Backing):
    provides = frozenset({"echo", "add"})
    props = frozenset({"name_upper"})
    gate = "ok"

    def echo(self, core, text):
        return text

    def add(self, core, a, b):
        return a + b

    def name_upper(self, core):
        return core.name.upper()


class Gated(Backing):
    provides = frozenset({"secret"})
    gate = "page"

    def applies(self, core):
        return core.unlocked          # only in play when unlocked

    def secret(self, core):
        return 42


class Thing(WebCore, BaseModel):
    name: str = "thing"
    unlocked: bool = False
    BACKINGS: ClassVar = (Always(), Gated())


def test_dispatch_calls_the_providing_backing():
    t = Thing()
    assert t.dispatch("echo", "hi") == "hi"
    assert t.dispatch("add", 2, 3) == 5


def test_property_op_dispatch():
    assert Thing(name="thing").dispatch("name_upper") == "THING"


def test_unknown_op_raises_unsupported():
    with pytest.raises(UnsupportedOp, match="not available"):
        Thing().dispatch("nope")


def test_capability_gating_by_state():
    locked = Thing(unlocked=False)
    assert not locked.has_op("secret")
    assert "page" not in locked.capabilities()
    with pytest.raises(UnsupportedOp):
        locked.dispatch("secret")

    unlocked = Thing(unlocked=True)
    assert unlocked.has_op("secret")
    assert "page" in unlocked.capabilities()
    assert unlocked.dispatch("secret") == 42


def test_choose_and_op_tables():
    t = Thing(unlocked=True)
    assert set(t.capabilities()) == {"ok", "page"}
    assert set(t.ops()) >= {"echo", "add", "secret"}
    assert "name_upper" in t.prop_ops()


def test_first_providing_backing_wins():
    class Override(Backing):
        provides = frozenset({"echo"})
        gate = "ok"

        def echo(self, core, text):
            return "override"

    class T2(WebCore, BaseModel):
        BACKINGS: ClassVar = (Override(), Always())

    assert T2().dispatch("echo", "x") == "override"
