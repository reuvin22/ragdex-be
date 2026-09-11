"""Chat payloads.

Note what is absent from every write shape: a sender, a timestamp, a thread id
that names someone else's conversation. The sender is the session, the clock is
the server's, and the thread is derived from the two participants. A client
that could set any of those could forge a message from another trader.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.directory import DirectoryEntry

MAX_MESSAGE_LENGTH = 2_000


class ChatMessage(BaseModel):
    id: str
    """The sender's uid. The client compares it against its own to pick a side."""
    sender: str
    text: str
    sent_at: datetime


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class NewContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(min_length=1, max_length=128)


class Contact(BaseModel):
    """Someone you have a conversation with, and the state of it."""

    person: DirectoryEntry
    unread: int = 0
    last_text: str = ""
    last_at: datetime | None = None
    """When they last had this thread open. What makes a sent message "seen"."""
    seen_at: datetime | None = None
    online: bool = False


class ContactList(BaseModel):
    contacts: list[Contact]


class Thread(BaseModel):
    """One conversation, as the dock renders it."""

    person: DirectoryEntry
    messages: list[ChatMessage]
    seen_at: datetime | None = None
    online: bool = False
