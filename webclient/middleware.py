"""Request middleware: `async (Request, next) -> Response`.

Retries, proxy rotation, caching and auth are all the same shape, so they are
one ordered, typed chain rather than logic smeared across the client, the
pool and the transport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .reference import Reference
from .telemetry import RedirectRecord, RequestRecord, RetryRecord, Telemetry


@dataclass
class Request:
    """One outbound request, after session and client defaults are merged."""

    reference: Reference
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    follow_redirects: bool = True
    max_redirects: int = 10
    telemetry: Telemetry | None = None


Next = Callable[[Request], Awaitable[Any]]
Middleware = Callable[[Request, Next], Awaitable[Any]]


def retry(attempts: int = 2, *, on_status: tuple[int, ...] = ()) -> Middleware:
    """Retry transport errors, and optionally named statuses. Deliberately
    minimal: no backoff, no jitter, no Retry-After. A real policy is a
    separate, deliberate piece of work — not something to grow by accident."""
    import httpx

    async def middleware(request: Request, call_next: Next) -> Any:
        last: BaseException | None = None
        for attempt in range(attempts + 1):
            try:
                response = await call_next(request)
            except httpx.TransportError as exc:
                last = exc
                if request.telemetry is not None:
                    request.telemetry.add(RetryRecord(
                        url=request.reference.url, attempt=attempt + 1,
                        reason=type(exc).__name__))
                continue
            if response.status_code in on_status and attempt < attempts:
                if request.telemetry is not None:
                    request.telemetry.add(RetryRecord(
                        url=request.reference.url, attempt=attempt + 1,
                        reason=f"status {response.status_code}"))
                continue
            return response
        assert last is not None
        raise last

    middleware.__name__ = f"retry({attempts})"
    return middleware


def redirects() -> Middleware:
    """Follow redirects ourselves, so every hop is a telemetry record and the
    final URL is unambiguous."""

    async def middleware(request: Request, call_next: Next) -> Any:
        current = request
        for _ in range(request.max_redirects + 1):
            response = await call_next(current)
            location = response.headers.get("location")
            if not (request.follow_redirects and location
                    and 300 <= response.status_code < 400):
                return response
            target = current.reference.join(location)
            if request.telemetry is not None:
                request.telemetry.add(RedirectRecord(
                    from_url=current.reference.url, to_url=target.url,
                    status=response.status_code))
            method = current.reference.method
            if response.status_code in (301, 302, 303) and method != "head":
                target = target.replace(method="get", body=None,
                                        json_body=None, form=None)
            current = Request(reference=target, headers=current.headers,
                              cookies=current.cookies, timeout=current.timeout,
                              follow_redirects=True,
                              max_redirects=current.max_redirects,
                              telemetry=current.telemetry)
        raise RecursionError(
            f"more than {request.max_redirects} redirects from "
            f"{request.reference.url}")

    middleware.__name__ = "redirects"
    return middleware


def observe() -> Middleware:
    """Record every request that actually goes out."""
    import time

    async def middleware(request: Request, call_next: Next) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        if request.telemetry is not None:
            request.telemetry.add(RequestRecord(
                url=request.reference.url,
                method=request.reference.method,
                status=response.status_code,
                ms=(time.perf_counter() - started) * 1000))
        return response

    middleware.__name__ = "observe"
    return middleware


def compose(stack: list[Middleware], terminal: Next) -> Next:
    """Fold a stack into one callable; the first entry is outermost."""
    call: Next = terminal
    for layer in reversed(stack):
        call = _bind(layer, call)
    return call


def _bind(layer: Middleware, call_next: Next) -> Next:
    async def bound(request: Request) -> Any:
        return await layer(request, call_next)
    return bound


def default_stack(attempts: int = 0) -> list[Middleware]:
    # redirects outermost, so every hop is retried and recorded
    return [redirects(), retry(attempts), observe()]
