"""The rules that stop one trader reading another's messages.

Same standing as ``test_security``: a regression here is a data breach. The
thread id is the whole isolation mechanism — it is derived from the caller's
uid plus the person they are addressing, and no route accepts one — so these
tests are about that derivation being what it claims to be.
"""

from __future__ import annotations

import pytest
from app.core.errors import AppError
from app.repositories.chat import thread_id
from app.repositories.directory import MIN_QUERY, search
from app.schemas.chat import NewMessage
from pydantic import ValidationError


def test_thread_id_is_the_same_from_either_side() -> None:
    """Both participants must derive one id, or each writes to their own copy
    of the conversation and neither sees the other."""
    assert thread_id("alice", "bob") == thread_id("bob", "alice")


def test_thread_id_is_specific_to_the_pair() -> None:
    assert thread_id("alice", "bob") != thread_id("alice", "carol")


def test_thread_id_cannot_be_confused_by_a_prefix() -> None:
    """A uid that is a prefix of another must not collide.

    Firebase uids are fixed-length so this cannot arise today, but the id is a
    security boundary and should not depend on that staying true.
    """
    assert thread_id("alice", "bob") != thread_id("alice", "bobby")


def test_short_searches_return_nothing(monkeypatch) -> None:
    """A one- or two-character prefix would return an arbitrary slice of the
    user base. That is enumeration however small the page is."""

    def explode():  # pragma: no cover - must never be reached
        raise AssertionError("A too-short search reached Firestore.")

    monkeypatch.setattr("app.repositories.directory._collection", explode)

    for term in ("", "a", "ab", "  ab  "):
        assert search(term, exclude_uid="anyone") == []

    assert MIN_QUERY == 3


def test_an_empty_message_is_refused() -> None:
    with pytest.raises(ValidationError):
        NewMessage(text="   ")


def test_an_oversized_message_is_refused() -> None:
    with pytest.raises(ValidationError):
        NewMessage(text="x" * 2_001)


def test_a_message_carries_nothing_but_text() -> None:
    """Sender and timestamp are the server's. A client that could set either
    could forge a message from the person it is talking to."""
    with pytest.raises(ValidationError):
        NewMessage(text="hello", sender="someone-else")  # type: ignore[call-arg]


def test_you_cannot_open_a_conversation_with_yourself() -> None:
    from app.repositories import chat

    with pytest.raises(AppError):
        chat.add_contact("alice", "alice")
