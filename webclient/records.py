"""`Record` and `RecordSet` — what `then` and `map` produce.

A Record captures success *or* failure, because `.otherwise()` is a method
called on the result of `.then()`: eagerly, `then` has already run by the time
`otherwise` is reached, so the failure has to be carried rather than raised.
Nothing is swallowed — a failed Record with no recovery raises the original
error the moment you touch its data.
"""
from __future__ import annotations

from typing import Any, Iterator, Mapping, Sequence

from .errors import WebClientError
from .values import Expr, ExprState, Failure, register_lazy_type


class Record(Expr, Mapping[str, Any]):
    """One projected record."""

    def __init__(self, data: Mapping[str, Any] | None = None, *,
                 failure: Failure | None = None, context: Any = None,
                 tagged: bool = False, ok: bool = True) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self._failure = failure
        self._context = context
        self._tagged = tagged
        self._ok = ok and failure is None
        self._expr: ExprState | None = None

    def _init_lazy(self) -> None:
        self._data, self._failure, self._context = {}, None, None
        self._tagged, self._ok = False, True

    # -- state ---------------------------------------------------------------
    @property
    def ok(self) -> bool:
        return self._ok

    @property
    def failure(self) -> Failure | None:
        return self._failure

    def _guard(self) -> dict[str, Any]:
        if self.is_lazy:
            raise WebClientError(
                "this Record is a lazy expression — call .collect() to run it")
        if self._failure is not None:
            error = self._failure.error
            raise WebClientError(
                f"this projection failed and was not recovered: "
                f"{self._failure.message}. Add .otherwise(...) to handle it."
            ) from error
        return self._data

    # -- Mapping -------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._guard()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._guard())

    def __len__(self) -> int:
        return len(self._guard())

    def get(self, key: str, default: Any = None) -> Any:   # type: ignore[override]
        return self._guard().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._guard())

    def _eager_value(self) -> Any:
        return self._data

    # `otherwise` on an already-evaluated Record uses the failure it carried,
    # through the same helper the plan evaluator uses — recovery means one
    # thing, implemented once.
    def _project(self, step: Any) -> Any:
        from .plan import OtherwiseStep
        if self.is_lazy or not isinstance(step, OtherwiseStep):
            return super()._project(step)
        from .engine import run_sync
        from .execute import Outcome, apply_otherwise, _finish
        outcome = Outcome(value=self if self._ok else None,
                          failure=self._failure, at=self._context)
        client = getattr(self._context, "_client", None)
        return _finish(run_sync(apply_otherwise(outcome, step, client, None)))

    def __hash__(self) -> int:
        raise TypeError("Record is not hashable")

    def __eq__(self, other: Any) -> Any:              # type: ignore[override]
        if self.is_lazy:
            return self._binop("eq", other)
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Record(<lazy {len(self._plan_or_new().steps)} steps>)"
        if self._failure is not None:
            return f"Record(failed: {self._failure.message!r})"
        return f"Record({self._data!r})"


class RecordSet(Expr, Sequence[Record]):
    """Many projected records — what `map` produces."""

    def __init__(self, rows: Sequence[Any] | None = None) -> None:
        self._rows: list[Any] = list(rows or ())
        self._expr: ExprState | None = None

    def _init_lazy(self) -> None:
        self._rows = []

    def _guard(self) -> list[Any]:
        if self.is_lazy:
            raise WebClientError(
                "this RecordSet is a lazy expression — call .collect()")
        return self._rows

    def __getitem__(self, index: Any) -> Any:         # type: ignore[override]
        return self._guard()[index]

    def __len__(self) -> int:
        return len(self._guard())

    def __iter__(self) -> Iterator[Any]:
        return iter(self._guard())

    def to_list(self) -> list[Any]:
        return [r.to_dict() if isinstance(r, Record) else r
                for r in self._guard()]

    @property
    def failures(self) -> list[Record]:
        return [r for r in self._guard()
                if isinstance(r, Record) and not r.ok]

    def _eager_value(self) -> Any:
        return self._rows

    def _project(self, step: Any) -> Any:
        """A recovery on a set applies to each failed row."""
        from .plan import OtherwiseStep
        if self.is_lazy or not isinstance(step, OtherwiseStep):
            return super()._project(step)
        out = []
        for row in self._rows:
            if isinstance(row, Record) and not row.ok:
                recovered = row._project(step)
                if recovered is not None:
                    out.append(recovered)
            else:
                out.append(row if step.recovery is None else row._project(step))
        return RecordSet(out)

    def __hash__(self) -> int:
        raise TypeError("RecordSet is not hashable")

    def __eq__(self, other: Any) -> Any:              # type: ignore[override]
        if self.is_lazy:
            return self._binop("eq", other)
        if isinstance(other, Sequence) and not isinstance(other, str):
            return self.to_list() == [
                dict(o) if isinstance(o, Mapping) else o for o in other]
        return NotImplemented

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"RecordSet(<lazy {len(self._plan_or_new().steps)} steps>)"
        return f"RecordSet({len(self._rows)} rows)"


register_lazy_type("Record", Record)
register_lazy_type("RecordSet", RecordSet)


# --------------------------------------------------------------------------- #
# `err` — the failure root inside a recovery block
# --------------------------------------------------------------------------- #

from .ops import bind_ops, op_property                      # noqa: E402


@bind_ops
class Err(Expr):
    """What `otherwise(...)` sees about the failure itself. `doc` in the same
    block is the context reached at the point of failure; `err` is the event."""

    def __init__(self, failure: Failure | None = None) -> None:
        self._failure = failure
        self._expr: ExprState | None = None

    def _init_lazy(self) -> None:
        self._failure = None

    @op_property(returns="Value")
    def message(self) -> Any:
        from .values import Value
        return Value(self._failure.message if self._failure else None)

    @op_property(returns="Value")
    def op(self) -> Any:
        from .values import Value
        return Value(self._failure.op if self._failure else None)

    @op_property(returns="Value")
    def kind(self) -> Any:
        from .values import Value
        return Value(self._failure.kind if self._failure else None)

    def _eager_value(self) -> Any:
        return self._failure

    def __repr__(self) -> str:
        if self.is_lazy:
            return "Err(<lazy>)"
        return f"Err({self._failure!r})"


register_lazy_type("Err", Err)
