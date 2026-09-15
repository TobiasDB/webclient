"""Collection and Field: the two surface types that carry a little behaviour.

Every other surface is a method-free wrapper over a core (``webclient.surface``),
but a ``Collection`` (a fanned-out set of results) and a ``Field`` (a scalar
leaf) are the sanctioned exceptions -- they hold minimal built-in helpers.
``Collection`` runs the row-shaping ops (``extract`` / ``filter`` / ``project``
/ ``documents``) by evaluating sub-expressions against each element; ``Field``
is the value leaf (``get`` + ``is_ok``/``is_empty`` + comparisons + truthiness).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Iterator, TypeVar, cast, overload

# covariant: Field/Collection/Lazy only ever *produce* T (iterate/index/get/collect),
# never consume it, so ``Collection[AsyncDocument]`` is a ``Collection[Document]``
# -- which lets the async surface override an inherited ``-> Collection[Document]`` op.
T = TypeVar("T", covariant=True)
M = TypeVar("M")  # a row model (e.g. a pydantic BaseModel) for project(model)

if TYPE_CHECKING:
    from .surfaces import Document, Reference


class Field(Generic[T]):
    """A scalar leaf: a value plus whether it is present/ok."""

    __slots__ = ("_value", "_ok")

    def __init__(self, value: Any = None, *, ok: bool = True) -> None:
        self._value = value
        self._ok = ok and value is not None

    def get(self, default: Any = None) -> T:
        return cast(T, self._value if self._ok else default)

    @property
    def value(self) -> T:
        return cast(T, self._value)

    @property
    def ok(self) -> bool:
        return self._ok

    def is_ok(self) -> "Field[bool]":
        return Field(self._ok)

    def is_empty(self) -> "Field[bool]":
        return Field(not self._ok or self._value in ("", [], {}, None))

    def __bool__(self) -> bool:
        return bool(self._value) if self._ok else False

    def __eq__(self, o: Any) -> bool:  # type: ignore[override]
        return bool(self.get() == (o.get() if isinstance(o, Field) else o))

    def __ne__(self, o: Any) -> bool:  # type: ignore[override]
        return not self.__eq__(o)

    def __hash__(self) -> int:
        return hash(self._value) if self._ok else 0

    def __repr__(self) -> str:
        return f"Field({self._value!r})" if self._ok else "Field(<empty>)"


def _raw(value: Any) -> Any:
    """Unwrap a Field to its raw value (missing -> None); pass anything else
    (a core column -- e.g. a ``Reference`` from ``attr("href")`` -- is kept so a
    later step can follow it; over the wire the service serialises it to a handle)."""
    return value.get() if isinstance(value, Field) else value


def _row_of(element: Any, *, create: bool = True) -> dict[str, Any] | None:
    """The extracted-columns dict on an element's core (a plain dict element is
    its own row). ``create`` seeds an empty row on first access."""
    if isinstance(element, dict):
        return element
    from .core.web_core import WebCore

    # a surface IS its core now (_row is a PrivateAttr on the concrete cores).
    core: Any = (
        element if isinstance(element, WebCore) else getattr(element, "_core", None)
    )
    if core is None:
        return None
    if core._row is None and create:
        core._row = {}
    return cast("dict[str, Any] | None", core._row)


async def apply_extract(element: Any, columns: dict[str, Any], client: Any) -> None:
    """Annotate ``element``'s row with the evaluated columns (unwrapped, stored in
    order so a later column can reference an earlier one). A missing field is
    ``None`` under RETURN, never an abort. THE one row-extraction implementation --
    shared by the eager (:meth:`Collection.aextract`) and streaming
    (``executor._astream_collection``) paths so they cannot diverge."""
    from .errors import RETURN, default_policy
    from .query.executor import aevaluate

    row = _row_of(element)
    if row is None:
        return
    with default_policy(RETURN):
        for key, expr in columns.items():
            row[key] = _raw(await aevaluate(expr, element, client=client))


async def survives_filters(element: Any, predicates: Any, client: Any) -> bool:
    """Whether ``element`` passes every predicate (each evaluated leniently). The
    one filter implementation, shared by eager and streaming paths."""
    from .errors import RETURN, default_policy
    from .query.executor import aevaluate, truthy

    with default_policy(RETURN):
        for pred in predicates:
            if not truthy(await aevaluate(pred, element, client=client)):
                return False
    return True


class Collection(Generic[T]):
    """A set of results (elements or rows). Iterable/indexable; the row-shaping
    ops evaluate sub-expressions per element, and element ops fan out."""

    __slots__ = ("_items", "_client", "name", "root")

    def __init__(
        self, items: list[Any] | None = None, *, client: Any = None, root: str = ""
    ) -> None:
        self._items = items or []
        self._client = client
        self.root = root
        self.name = f"col:{root}" if root else "col:"

    # -- container ------------------------------------------------------------
    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i: int) -> T:
        return cast(T, self._items[i])

    def __repr__(self) -> str:
        return f"Collection({len(self._items)} items)"

    if TYPE_CHECKING:
        # >>> generated: collection element-op lifting <<<
        # fmt: off
        def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "Collection[Field[str]]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "Collection[Document]": ...
        def is_empty(self) -> "Collection[Field[bool]]": ...
        def is_ok(self) -> "Collection[Field[bool]]": ...
        def message(self) -> "Collection[Field[str]]": ...
        def ref(self) -> "Collection[Reference]": ...
        def reload(self) -> "Collection[Document]": ...
        def render(self, format: str, **options: Any) -> "Collection[Field[str]]": ...
        def screenshot(self, selector: str | None = ...) -> "Collection[Document]": ...
        def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "Collection[Document]": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "Collection[Document]": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "Collection[Document]": ...
        # fmt: on
        # >>> end generated <<<
    else:

        def __getattr__(self, name: str) -> Any:
            """An element op fans out over the elements: a list of results (a
            Collection when the results are surfaces)."""
            if name.startswith("_"):
                raise AttributeError(name)

            def fan(*args: Any, **kwargs: Any) -> Any:
                from .core.web_core import WebCore

                def apply(el: Any) -> Any:
                    attr = getattr(el, name)
                    # a prop op already resolved to its value (not callable); a
                    # call op returns a dispatcher we invoke with the args. Works
                    # for a core surface and a remote handle alike.
                    return attr(*args, **kwargs) if callable(attr) else attr

                results = [apply(el) for el in self._items]
                if results and all(isinstance(r, WebCore) for r in results):
                    return Collection(results, client=self._client, root=self.root)
                return results

            return fan

    # -- row shaping ----------------------------------------------------------
    def _loop(self) -> Any:
        from .core.client import default_client

        return (self._client or default_client()).loop()

    async def aextract(self, **exprs: Any) -> "Collection[T]":
        """Annotate each element with extracted columns (its ``_row``): columns
        are evaluated in order against the element (a later column can reference
        an earlier one via ``field``; chained extracts accumulate); elements are
        evaluated concurrently, bounded by the pool. Fields store unwrapped."""
        from .query.executor import fan_out

        async def one(el: Any) -> None:
            await apply_extract(el, exprs, self._client)

        await fan_out(list(self._items), one, limit=self._limit())
        return self._derive(self._items)

    async def afilter(self, *predicates: Any) -> "Collection[T]":
        """Keep the elements for which every predicate is truthy."""
        from .query.executor import fan_out

        async def keep(el: Any) -> bool:
            return await survives_filters(el, predicates, self._client)

        flags = await fan_out(list(self._items), keep, limit=self._limit())
        kept = [el for el, ok in zip(self._items, flags) if ok]
        return self._derive(kept)

    def _limit(self) -> int:
        from .query.executor import _fanout_limit

        return _fanout_limit(self._client)

    def extract(self, **exprs: Any) -> "Collection[T]":
        """Eager form of :meth:`aextract` (bridged onto the engine loop)."""
        return cast("Collection[T]", self._loop().run(self.aextract(**exprs)))

    def filter(self, *predicates: Any) -> "Collection[T]":
        """Eager form of :meth:`afilter` (bridged onto the engine loop)."""
        return cast("Collection[T]", self._loop().run(self.afilter(*predicates)))

    def documents(self, column: str) -> "Collection[Any]":
        """Flatten a column whose values are Collections/lists of documents
        into one Collection of those documents."""
        out: list[Any] = []
        for el in self._items:
            value = (_row_of(el) or {}).get(column)
            out.extend(list(value) if value is not None else [])
        return self._derive(out)

    def limit(self, n: int) -> "Collection[T]":
        """Keep at most the first ``n`` elements."""
        return self._derive(self._items[:n])

    @overload
    def project(self) -> list[dict[str, Any]]: ...
    @overload
    def project(self, model: type[M]) -> list[M]: ...

    def project(self, model: type[M] | None = None) -> list[Any]:
        """Materialise as a plain list: each element's extracted row if it has
        one, else the element itself. Pass ``model`` (e.g. a pydantic model) to
        validate each row into it -- a schema-guided, typed result. Eager only:
        a model class is not part of the serialisable plan, so call it on a
        materialised Collection (``...extract(...).collect().project(Model)``)."""
        out: list[Any] = []
        for el in self._items:
            row = _row_of(el, create=False)
            out.append(row if row is not None else el)
        if model is None:
            return out
        validate = getattr(model, "model_validate", None)
        return [validate(r) if validate is not None else model(**r) for r in out]

    def _derive(self, items: list[Any]) -> "Collection[T]":
        out: Collection[T] = Collection(items, client=self._client, root=self.root)
        out.name = self.name
        return out


__all__ = ["Collection", "Field"]
