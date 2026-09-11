"""The live stream: that events reach the client, and that listeners stop.

Driven through the response's body iterator rather than an HTTP client. A
server-sent stream never ends on its own, and a TestClient request that waits
for one to finish simply deadlocks — so the endpoint is called directly and its
iterator pulled one event at a time, which tests the part that carries the risk
without needing a socket.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.api.v1.routes import chat as route
from app.core.middleware import SelectiveGZipMiddleware
from app.core.security import CurrentUser
from app.repositories.chat import other_member


class FakeWatch:
    """Stands in for a Firestore listener, and records being cancelled."""

    def __init__(self) -> None:
        self.unsubscribed = False

    def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeSnapshot:
    def __init__(self, members: list[str]) -> None:
        self._data = {"members": members}

    def to_dict(self) -> dict[str, Any]:
        return self._data


class FakeRequest:
    """Only the one method the endpoint uses."""

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.fixture
def caller() -> CurrentUser:
    return CurrentUser(
        uid="trader-1", email="t@example.com", email_verified=True, name="T"
    )


async def test_a_thread_change_becomes_an_event(caller, monkeypatch) -> None:
    """The point of the endpoint: a Firestore change reaches the client.

    The real listener fires on a gRPC background thread; what matters is that
    the handover reaches the event loop, so the callback is invoked directly.
    """
    watch = FakeWatch()
    captured: dict[str, Any] = {}

    def fake_watch_threads(me: str, on_change: Any) -> FakeWatch:
        captured["on_change"] = on_change
        return watch

    monkeypatch.setattr(route.repo, "watch_threads", fake_watch_threads)

    request = FakeRequest()
    response = await route.stream(caller, request)  # type: ignore[arg-type]

    assert response.media_type == "text/event-stream"
    # nginx and several CDNs buffer proxied responses by default, which would
    # hold every event until the connection closed.
    assert response.headers["x-accel-buffering"] == "no"

    events = response.body_iterator

    assert await anext(events) == ": open\n\n"

    # The event names the *other* participant, not the caller.
    captured["on_change"]([FakeSnapshot(["trader-1", "mara"])], [], None)

    assert await anext(events) == 'event: change\ndata: {"uid": "mara"}\n\n'

    # Disconnecting must take the Firestore listener with it, or a closed tab
    # leaves a subscription running for the life of the process. The check sits
    # just after the yield, so the next pull is the one that ends the stream.
    request.disconnected = True
    captured["on_change"]([FakeSnapshot(["trader-1", "mara"])], [], None)

    with pytest.raises(StopAsyncIteration):
        await anext(events)

    assert watch.unsubscribed


async def test_a_quiet_stream_sends_a_heartbeat(caller, monkeypatch) -> None:
    """Proxies close idle connections. A silent stream has to say something."""
    monkeypatch.setattr(route.repo, "watch_threads", lambda me, cb: FakeWatch())
    monkeypatch.setattr(route, "_HEARTBEAT_SECONDS", 0.01)

    response = await route.stream(caller, FakeRequest())  # type: ignore[arg-type]
    events = response.body_iterator

    assert await anext(events) == ": open\n\n"
    assert await asyncio.wait_for(anext(events), timeout=2) == ": keep-alive\n\n"

    await response.body_iterator.aclose()


def test_a_malformed_thread_names_nobody() -> None:
    """A document without exactly two members is not a conversation, and must
    not produce an event pointing at an arbitrary uid."""
    assert other_member({"members": ["a", "b"]}, "a") == "b"
    assert other_member({"members": ["a"]}, "a") is None
    assert other_member({"members": ["a", "b", "c"]}, "a") is None
    assert other_member({}, "a") is None


async def test_gzip_is_skipped_for_the_stream_path() -> None:
    """Compression buffers a streaming response, which is the difference
    between a live stream and one that arrives all at once at the end."""
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["path"])

    middleware = SelectiveGZipMiddleware(
        app, minimum_size=1, exclude_prefixes=("/api/v1/chat/stream",)
    )

    async def receive() -> Any:  # pragma: no cover - never called
        return {"type": "http.request"}

    async def send(message: Any) -> None:  # pragma: no cover - never called
        return None

    scope = {
        "type": "http",
        "path": "/api/v1/chat/stream",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    await middleware(scope, receive, send)

    # Reached the app directly: no gzip responder in between.
    assert seen == ["/api/v1/chat/stream"]
