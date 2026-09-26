"""Coaching: invitations, rosters, and what a coach may know.

The figures on a student are derived here from their journal, never sent by
the client — the same rule the trader's own statistics follow. A coach reading
a roster is reading arithmetic this service did.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

#: Where an invitation has got to.
#:
#: ``declined`` is kept rather than deleted, so a coach cannot re-invite
#: somebody who said no simply by asking again — the row is still there and the
#: write is an overwrite of a refusal, which is visible.
EnrolmentStatus = Literal["pending", "active", "declined"]

MAX_NOTE = 300


class InviteRequest(BaseModel):
    """Invite by email address, the way contact search works.

    An address and not a uid: a uid is not something one trader can know about
    another, and asking for one would mean handing out a way to address
    accounts directly.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    note: str = Field(default="", max_length=MAX_NOTE)


class Invitation(BaseModel):
    """An invitation as the student sees it — who is asking, and why."""

    coach_uid: str
    coach_name: str = ""
    coach_email: str = ""
    coach_photo: str = ""
    note: str = ""
    invited_at: datetime | None = None


class InvitationList(BaseModel):
    invitations: list[Invitation]


class SentInvite(BaseModel):
    """An invitation as the coach who sent it sees it."""

    student_uid: str
    student_name: str = ""
    student_email: str = ""
    student_photo: str = ""
    status: EnrolmentStatus
    note: str = ""
    invited_at: datetime | None = None


class SentInviteList(BaseModel):
    invites: list[SentInvite]


class StudentSummary(BaseModel):
    """One student on a roster.

    Everything below ``photo_url`` is computed from their journal by this
    service. A client cannot supply any of it, and a coach without an accepted
    enrolment cannot read any of it.
    """

    uid: str
    display_name: str = ""
    email: str = ""
    photo_url: str = ""
    since: datetime | None = None

    trade_count: int = 0
    closed_count: int = 0
    win_rate: float = 0.0
    net_pl: float = 0.0
    #: Rules followed, as a percentage, or null when nothing has been graded.
    rule_score: int | None = None
    last_trade_at: datetime | None = None


class StudentList(BaseModel):
    students: list[StudentSummary]


class CoachSummary(BaseModel):
    """The person teaching you."""

    uid: str
    display_name: str = ""
    email: str = ""
    photo_url: str = ""
    since: datetime | None = None


class MyCoach(BaseModel):
    """Null rather than a 404: having no coach is an ordinary answer."""

    coach: CoachSummary | None = None


class EmailTemplate(BaseModel):
    """The invitation a coach composes, in three parts.

    Header, body and footer rather than one blob, because a mail shell has to
    own the outer table and the parts have different jobs: a header is branding,
    a footer is the small print, and only the body is the message. Splitting
    them also means the shell can put the accept button between body and footer
    without an author having to leave a hole for it.

    Everything is sanitised on the way in — see ``views/mail_html.py``. What is
    stored is already what will be sent.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(default="", max_length=200)
    header_html: str = Field(default="", max_length=20_000)
    body_html: str = Field(default="", max_length=20_000)
    footer_html: str = Field(default="", max_length=20_000)
    #: The one colour the shell takes from the coach, for the button and rules.
    accent: str = Field(default="", max_length=32)


class UniversitySettings(BaseModel):
    """A coach's programme, as they describe it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=120)
    blurb: str = Field(default="", max_length=300)
    template: EmailTemplate = Field(default_factory=EmailTemplate)
