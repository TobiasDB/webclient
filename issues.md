Lazy branching conditioning needs to be closer to Polars -> wc.when().then().otherwise() not methods on Field.
Filter can become wc.filter()
We can drop custom Field & Collection these fall out as python expressions e.g.

lazy = [
    doc.extract(...)
    for doc in doc.select_all(...)
].collect()

Default mode is Lazy -> Everything is lazy and requires a user to call .collect()
Other mode is Eger -> Everything os instantly collected e.g. select_all(_collect=True) -> This re uses the op mechanism for _error handling etc.

The motivation here is that Document/WebClient/WebSession become complete SHIM interfaces - i.e. could literally be ipy as they are just used to build the expressions. The WebClientCore / DocumentCore are responsible for evaluating them, either eger or lazy.

This also means that the remote backend could open a websocket and egerly evaluate expressions to have the same interface as the local webclient.

This is looking to solve several problems
1. Document/Webclient/Session are already very shallow and dont do anything other than calling a backend -> They can build expressions and return them, decorators and wrappers can do the work -  a single function to handle all of our dispatching
2. It is confusing when we are working with lazy objects vs real objects -> this allows us to work in a way to be clear about this.
3. The remote client shares the same methods as the normal webclient s.t. they are interchangeable without any code change



class Collection[LazyT, T](Protocol):
    def collect() -> list[T

class LazyDocument(Protocol):
    ...
    def collect() -> Document: ...

class Document(LazyDocument)
    status_code ...
    content: ...

    (all methods stay lazy from the LazyDocument)
    (in eagar mode LazyDocument -> Document, without collect, so we need to update the stubs for eager mode some how - this is the interesting part)