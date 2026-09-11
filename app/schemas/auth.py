"""Sign-in payloads.

A password arrives here and goes no further than Identity Toolkit. It is never
written to a document, never logged — the request logger records method, path
and status, not bodies — and never returned.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Firebase's own floor is six characters. Matching it exactly means a rejection
# comes from us with a readable message rather than from Identity Toolkit as
# WEAK_PASSWORD after a round trip.
_MIN_PASSWORD = 6
_MAX_PASSWORD = 200


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=_MIN_PASSWORD, max_length=_MAX_PASSWORD)


class Registration(Credentials):
    display_name: str = Field(default="", max_length=80)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class SessionUser(BaseModel):
    """Who the caller is, as the client needs them.

    Deliberately not the account record: this is identity only, and the client
    reads it on every load to decide between the app, the login screen and the
    verify-email gate.
    """

    uid: str
    email: str | None = None
    email_verified: bool = False
    display_name: str = ""
    photo_url: str = ""
    # Provider ids, e.g. "google.com". The verify-email gate words itself
    # differently for an account that arrived through Google.
    providers: list[str] = Field(default_factory=list)


class Session(BaseModel):
    """The answer to "who am I". ``user`` is null when nobody is signed in."""

    user: SessionUser | None = None


class VerificationSent(BaseModel):
    status: str
