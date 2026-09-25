"""Contact search payloads."""

from __future__ import annotations

from pydantic import BaseModel


class DirectoryEntry(BaseModel):
    """One person, as contact search sees them.

    Four fields, and no more: this is the only shape in which one trader's
    details reach another. Anything added here becomes visible to anyone who
    can guess an email address.
    """

    uid: str
    email: str = ""
    display_name: str = ""
    photo_url: str = ""


class DirectoryResults(BaseModel):
    results: list[DirectoryEntry]
