"""Documents a coach puts in front of their students.

Two kinds, one shape. An *agreement* is prose somebody signs; a *form* is
questions somebody answers. They share a list, a permission model and a
submission record, so they are one collection with a discriminator rather than
two of everything — the alternative is two screens, two endpoints and two sets
of the same enrolment check.

What a student sends back is the only place in this service where one account
writes data another account reads. The rules that make that safe live in the
repository and the routes; what is fixed *here* is the shape, and in
particular that a submission carries no identity the sender chose — the uid
comes from the session, and the document it answers comes from the path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: What a document is for.
#:
#: ``agreement`` is read and signed. ``form`` is answered. Nothing is both: a
#: form with a signature line would make it unclear which of the two a
#: submission is evidence of.
DocumentKind = Literal["agreement", "form"]

#: How a question is answered.
#:
#: Short on purpose. Every type here is one a coach can explain in a sentence
#: and a student can answer on a phone; anything more elaborate is a form
#: builder, which is a product rather than a feature.
QuestionType = Literal[
    "short_text", "long_text", "single_choice", "multi_choice", "scale"
]

MAX_QUESTIONS = 40
MAX_CHOICES = 12
MAX_BODY = 20_000
MAX_ANSWER = 4_000

#: The scale used by ``scale`` questions, so the client and the validator
#: cannot disagree about what "5" means.
SCALE_MIN = 1
SCALE_MAX = 5


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Stable across edits, because answers are keyed by it. Minted by the
    #: server on create so two clients cannot choose the same one.
    id: str = Field(default="", max_length=64)
    type: QuestionType
    prompt: str = Field(min_length=1, max_length=500)
    help_text: str = Field(default="", max_length=300)
    required: bool = False
    #: Only for the choice types; ignored otherwise.
    choices: list[str] = Field(default_factory=list, max_length=MAX_CHOICES)


class DocumentWrite(BaseModel):
    """What a coach sends when creating or editing one."""

    model_config = ConfigDict(extra="forbid")

    kind: DocumentKind
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=400)
    #: The prose of an agreement. Sanitised on the way in, like the email
    #: template — a student reads this rendered, so it is markup somebody else
    #: supplied being shown in somebody's browser.
    body_html: str = Field(default="", max_length=MAX_BODY)
    questions: list[Question] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    #: Whether every student is expected to complete it.
    required: bool = True
    #: Off until the coach is ready. A draft is invisible to students.
    published: bool = False
    #: The form an invited trader fills in before they are enrolled.
    #:
    #: At most one per coach — setting it on a document clears it on the rest,
    #: because "the intake form" has to name one thing. It is also the only
    #: document readable by somebody who is *not* yet a student, which is why
    #: it is a flag here rather than a convention about the title.
    is_intake: bool = False


class UniversityDocument(BaseModel):
    """One document, as either side sees it."""

    id: str
    coach_uid: str
    kind: DocumentKind
    title: str
    summary: str = ""
    body_html: str = ""
    questions: list[Question] = Field(default_factory=list)
    required: bool = True
    published: bool = False
    is_intake: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    #: How many students have completed it. Only filled in for the coach —
    #: a student has no business knowing who else has signed what.
    submission_count: int | None = None
    #: The caller's own submission, when they are a student and have made one.
    submitted_at: datetime | None = None


class DocumentList(BaseModel):
    documents: list[UniversityDocument]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=64)
    #: One string for the text and single-choice types; a list for
    #: ``multi_choice``; the number as a string for ``scale``. Kept as text so
    #: a question whose type changes does not orphan the answers under it.
    value: str = Field(default="", max_length=MAX_ANSWER)
    values: list[str] = Field(default_factory=list, max_length=MAX_CHOICES)


class SubmissionWrite(BaseModel):
    """What a student sends back.

    No uid and no timestamp. Both come from the server — a submission that
    could name its own author is not evidence of anything.
    """

    model_config = ConfigDict(extra="forbid")

    #: For an agreement: the name typed as a signature.
    signed_name: str = Field(default="", max_length=200)
    #: For a form.
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_QUESTIONS)


class Submission(BaseModel):
    document_id: str
    student_uid: str
    student_name: str = ""
    student_email: str = ""
    signed_name: str = ""
    answers: list[Answer] = Field(default_factory=list)
    submitted_at: datetime | None = None
    #: A digest of what was signed, taken at submission time.
    #:
    #: Without it, "they agreed to this" means "they agreed to whatever this
    #: document says now" — and the coach can edit it afterwards. With it, an
    #: edited agreement no longer matches its own signatures, which is the
    #: whole point of recording one.
    document_digest: str = ""


class SubmissionList(BaseModel):
    submissions: list[Submission]
    #: Students on the roster who have not completed it.
    outstanding: list[str] = Field(default_factory=list)
