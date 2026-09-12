"""
All webclient types specify a 
name - A identifier for the object auto generated e.g. doc:000-001 or ref:000-001
       The ID is intentinally short / human readable / low entropy so it can easily be referenced
       by a human or LLM.
root - A reference to object from which this object was created or is bound to. 
       This form a part of a reference chain using object names 
       e.g. wc:session_id -> ref:ref_id -> doc:doc_id -> doc:doc_id  
kind - A identifier for the kind of object e.g. for references: webpage, api, document, ...
        for documents: binary, html, xml, json, excel, csv, parquet, pdf
        The underlying Class does not change 

The purpose of these fields, any object is identifiable and the relationship between them is known 
and exploitable. i.e.

Given a ref.resolve() or wc.resolve(ref) we could later recover it with
wc.reference('id') or wc.document('id')
"""
from __future__ import annotations
from typing import Self, Any


class Reference:
    """
    Encapsulates all the information required to reference ANY specific object on the web.
    e.g. A Website, an element on a website, a pw render state of a website, a pdf, an API call.
    Therefore it must record the scheme://host:port/path?query#fragment + headers + cookies + auth (reference) + actions.

    Actions are any actions taken from a Document s.a. selecting an element, clicking a button,
    Actions are stored as a Lazy chain. 

    References should be ref->json->ref s.t. json is minimal.

    References are 'resolved' by the WebClient.

    """
    name: str
    kind: ...
    root: ...

    ...
    params: dict[str, Any]

    def resolve(self, *args, **kwargs) -> Document: ...


# Each WebBase method has a error handling policy, s.t. an object can always be returned
IGNORE = "error-ignore"  # .err = Exception .ok = True .message = "Ignored ..."
RETURN = "error-return"  # .err = Exception .ok = False .message = "Exception ..."
RAISE = "error-raise"  # Raises instantly
# For methods that dont return a WebBase object error-ignore -> None error-return -> Exception error-raise -> Raises
# For methods that return Self, these values are updated in place
# This can be implemented as a decorator to avoid bloating the code base

class WebBase:
    name: str
    kind: ...
    root: ...

    created: int
    updated: int
    accessed: int

    error: Exception | None
    message: str | None
    ok: bool

    # Extract evaluates a number of expressions against the Object and names their output
    # These can later be referenced by name from this document e.g. field, reference, document.
    # For a collection extract is like a Map
    # Collections will need special handling of these methods to un-nest, and map correctly
    # The type hints should also be updated
    def extract(self, **named_expr) -> Self: ...

    # Project projects all fields from this document & sub documents into a nested dict
    # If a model is provided it is called with this dict as input.
    def project[T](self, model: type[T] | None = None) -> T | dict: ...

    # Any events bound to this object during its lifecycle
    def events(self, kind: type[Event] | None = None) -> Event: ...

    def is_empty(self) -> bool: ...
    def is_ok(self) -> bool: ...

    def field(self, name: str) -> str | int | float | bool: ...
    def fields(self, *names: str) -> dict[str, str | int | float | bool]: ...
    def reference(self, name: str) -> Reference: ...
    def references(self, *names: str) -> Collection[Reference]: ...
    def document(self, name: str) -> Document: ...
    def documents(self, *names: str) -> Collection[Document]: ...


class Collection[T](WebBase):
    """
    A Collection represents a group of objects and is a mix between a dict & list.
    """
    def filter(self, *expr, **named_expr) -> Collection[T]: ...



class Document(WebBase):
    """
    Provides a single surface for interacting with a resolved reference 
    """
    url: str
    status_code: int
    content_size: int
    content: bytes
    cookies: dict[str, str]
    headers: dict[str, str]

    # Return The reference to this object self.root unless mutated by stateful actions e.g. click, write, wait
    def ref(self) -> Reference: ...

    # @require("tree")
    def select(self, *args, **kwargs) -> Document: ...
    def select_all(self, *args, **kwargs) -> Collection[Document]: ...
    def attr(self, *args, **kwargs) -> str | int | float | bool | Reference: ...

    # @require("page")
    def click(self, *args, **kwargs) -> Self: ...
    def write(self, *args, **kwargs) -> Self: ...
    def press(self, *args, **kwargs) -> Self: ...
    def wait(self, *args, **kwargs) -> Self: ...
    def hover(self, *args, **kwargs) -> Self: ...
    def scroll(self, *args, **kwargs) -> Self: ...

    def screenshot(self, *args, **kwargs) -> Document: ...


class Event: ...
class NetworkEvent(Event): ...
class DomEvent(Event): ...
class ConsoleEvent(Event): ...
class ActionEvent(Event): ...

class ExprMeta:
    # Used to trick the static type checker
    def __getitem__[T](cls, key: type[T]) -> T:
        return cls(key, "_ref_")


class Expr(metaclass=ExprMeta):
    def __init__(self, parent: Expr | None, root: type, op: str, *args, **kwargs):
        self.parent = parent
        self.root = root
        self.op = op
        self.args = args
        self.kwargs = kwargs

    # The return types should be resolved for Expr validation -> The static type checker is tricked already but we should 
    # Validate the expressions
    # These functions require special attention / should use a well build lib to extract this information 
    # There are lots of type hints - Technically validation is not needed but is nice to have
    # Example edge cases, BaseModel getattr will fail and need to check root.model_fields
    # Properties need _call_return_type
    def __getattr__(self, *args, **kwargs): 
        return_type = _attr_return_type(_self=self.root, root=self.root, name=args[0])
        return Expr(self, return_type, "__getattr__", *args, **kwargs)

    # More edge cases here
    # Is a class.__init__ e.g. ref(...)
    # Is overloaded (need to find the sig that matches the call args for the correct return type)
    # Has no return type -> Any
    # Has self return type -> self
    # Uses generics (cam use a runtime hack)
    # class X:; obj: _self; kind = typing.get_type_hint(X, localns={"_self": _self})["x"]
    # Is a union of types -> we need the most common base type or Any
    # Is list/sequence
    # Ideally we have a lib for all these type validation stuff
    def __call__(self, *args, **kwargs):
        return_type = _call_return_type(_self=self.parent.root if self.parent else None, root=self.root, *args, **kwargs)
        return Expr(self, return_type, "__call__", *args, **kwargs)

    ... # and all other built-in methods

doc = Expr[Document]
many = Expr[Collection[Any]]
ref = Expr[Reference]
evt = Expr[Event]

# IMPORTANT: Notice how Expr is completly independent of the Document | Reference | Collection | Event models
# and any class can be made lazy with Expr[Lazy]
# These expressions are also serialazable (so long as the arguments are) and the resulting chain can be turned into JSON
# Executed as a plan & subplan.
# Also notice how the static type hints just work the same as if we were using the Real objects.

def is_empty(expr: Expr) -> Expr: ...

multi_expr = (
    doc.select_all(...),  # -> Collection[Document]
    many.extract(
        title=doc.select("div.title").attr("text"),
        children=doc.select_all("li.row"),
    ),
    many.filter(is_empty(doc.field("title"))),

    # Here the Collection[Document].element("children") -> Collection[Document] i.e flatterns the Collection
    # Collection[Document].select("a").attr("href")
    many.document("children").select("a").attr("href"), # -> Collection[Reference]
)

# wc.execute(*multi_expr, doc=some_resolved_document) -> Collection[Reference]

from pydantic import BaseModel
import polars as pl
SearchResult: dict | BaseModel | pl.DataFrame = dict

search_expr = (
    Reference("search engine url")
    .resolve() # Since reference is not bound to a webclient, this returns a lazy object
    .select_all("div.result", limit=5)
    .extract(
        title=doc.select("a.result__a").attr("text"),
        description=doc.select(".result__snippet").attr("text"),
        url=doc.select("a.result__a").attr("href").params.get("uddg"),
    )
    .project(SearchResult)
)

# wc.execute(search_expr) -> list[SearchResult]


SummaryResult: dict | BaseModel | pl.DataFrame = dict
summary_expr = (
    Reference("some url")
    .resolve(browser=True, anti_bot=True, proxy=True)
    .wait("domstable", error=IGNORE)
)

# wc.execute(summary_expr) -> Collection[Reference]





"""
The webclient  interface then becomes very simple


"""

class WebClientCore:
    """
    Manages Clients(browser, http, llm, etc)/Sessions(id)/Resolve(reference)/Execute(plan)/Events

    Resolve is the core function here
    It decides how to create the Document, what bindings it has and manages its lifecycle
    i.e. when the tree is created, when the page is created and cleaned up, which events are collected
    for which document

    This should be fully Async
    """

class WebClient(WebClientCore):
    """
    The WebClient then becomes a shallow sync interface with user facing functions
    All functions should just build an expression and use the WebClientCore to execute it
    Expressions are something that can be executed by a asyncrounous engine or syncrounous
    since building expressions is near instant, this makes the interface change between the two simple
    And also between the remote client / API interface.
    This is also the thing that gets converted into the API / RemoteClient
    """
    def session(self): ...
    def document(self, id): ...
    
    def fetch(self, **reference_like) -> Document: ...

    # Implementation of these is defered but the idea is present
    def search(self, search_term) -> ...: ...
    def summary(self, **reference_like) -> ...: ...
    def crawl(self, term = None, seeds = None) -> ...: ...

