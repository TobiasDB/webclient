"""P1 gate: the error-policy decorator (PLAN Decision 7), now a plain
``@policy`` on real methods -- no registry, no recording here."""
import pytest
from typing_extensions import Self

from webclient import IGNORE, RAISE, RETURN, Field, WebBase
from webclient.core.base import OpError, UnsupportedOperation, default_policy, policy


class Thing(WebBase):
    hits: int = 0

    @policy(returns="Self")
    def bump(self, fail: bool = False) -> Self:
        if fail:
            raise ValueError("boom")
        self.hits += 1
        return self

    @policy(returns="Field")
    def val(self, fail: bool = False) -> Field[int]:
        if fail:
            raise LookupError("nothing")
        return Field[int](value=self.hits)

    @policy(returns="Field", require="page")
    def needs_page(self) -> Field[int]:
        return Field[int](value=1)


def test_eager_default_is_raise():
    with pytest.raises(ValueError, match="boom"):
        Thing().bump(fail=True)
    with pytest.raises(LookupError):
        Thing().val(fail=True)


def test_return_gives_not_ok_object():
    t = Thing(name="t:1").bump(fail=True, error=RETURN)
    assert t.ok is False and t.error is not None
    assert t.error.type == "ValueError" and t.message == "ValueError: boom"
    f = Thing(name="t:1").val(fail=True, error=RETURN)
    assert isinstance(f, Field) and f.ok is False and f.root == "t:1"
    with pytest.raises(OpError, match="LookupError: nothing"):
        f.get()


def test_ignore_is_ok_but_keeps_diagnostics():
    t = Thing().bump(fail=True, error=IGNORE)
    assert t.ok is True and t.error is not None
    assert t.message == "Ignored ValueError: boom"
    assert t.is_ok().get() is True
    f = Thing().val(fail=True, error=IGNORE)
    assert f.ok and f.get() is None and f.is_empty().get() is True


def test_success_clears_ignored_error():
    t = Thing().bump(fail=True, error=IGNORE)
    assert t.error is not None
    t.bump()
    assert t.error is None and t.message is None and t.ok and t.hits == 1
    assert t.updated > 0 and t.accessed > 0


def test_not_ok_receiver_short_circuits():
    t = Thing().bump(fail=True, error=RETURN)
    with pytest.raises(OpError, match="ValueError: boom"):
        t.bump()
    assert t.bump(error=RETURN) is t and t.hits == 0      # did not execute
    f = t.val(error=RETURN)
    assert f.ok is False and f.error is t.error            # same failure
    assert t.is_ok().get() is False                        # always-ops run
    assert bool(t.is_ok()) is False


def test_capability_checked_every_call_with_a_fix_in_the_message():
    with pytest.raises(UnsupportedOperation, match="requires 'page'"):
        Thing().needs_page()
    inspected = Thing().needs_page(error=RETURN)
    assert inspected.ok is False and "page" in inspected.message


def test_default_policy_context_flips_the_default():
    with default_policy(RETURN):
        t = Thing().bump(fail=True)               # no explicit error=
    assert t.ok is False and t.error.type == "ValueError"
    assert Thing().bump(fail=True, error=IGNORE).ok is True   # explicit wins
