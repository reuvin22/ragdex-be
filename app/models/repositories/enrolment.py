"""Who coaches whom.

One document per coach-student pair, keyed ``<coachUid>_<studentUid>``. The
key is derived from the two parties rather than generated, which buys three
things: inviting the same person twice overwrites instead of duplicating,
accept and decline can address a row without the client holding an id, and a
membership check is a document read rather than a query.

**This is the grant that lets a coach read a student's journal.** Nothing else
in the service permits one account to read another's trades, so
``is_active`` below is the whole of that permission and every read of another
uid goes through it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.db.firestore import enrolments_collection
from app.models.schemas.university import EnrolmentStatus

#: A coach cannot hold an unbounded roster through this API. Firestore would
#: serve more; the cap is here because every student on a roster costs a
#: journal read to summarise, and an unbounded roster is an unbounded response.
MAX_ROSTER = 100


def key_for(coach_uid: str, student_uid: str) -> str:
    return f"{coach_uid}_{student_uid}"


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


class Enrolment:
    """A row, flattened into something the services can read."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.coach_uid = str(data.get("coachUid", ""))
        self.student_uid = str(data.get("studentUid", ""))
        raw = data.get("status")
        self.status: EnrolmentStatus = (
            raw if raw in ("pending", "active", "declined") else "pending"
        )
        self.note = str(data.get("note", ""))
        self.invited_at = _to_datetime(data.get("invitedAt"))
        self.applied_at = _to_datetime(data.get("appliedAt"))
        self.responded_at = _to_datetime(data.get("respondedAt"))


def get(coach_uid: str, student_uid: str) -> Enrolment | None:
    snapshot = enrolments_collection().document(key_for(coach_uid, student_uid)).get()
    if not snapshot.exists:
        return None
    return Enrolment(snapshot.to_dict() or {})


def is_active(coach_uid: str, student_uid: str) -> bool:
    """Whether this coach may read this student.

    One document read, and the only thing standing between a coach and
    somebody else's journal. Deliberately not a query: a query that returned
    nothing because an index was missing would read as "no permission", and
    this is not a check that should be able to fail open or closed by accident.
    """
    found = get(coach_uid, student_uid)
    return found is not None and found.status == "active"


def invite(coach_uid: str, student_uid: str, *, note: str) -> Enrolment:
    """Offer a place. Re-inviting somebody refreshes the same row.

    An accepted enrolment is left alone: re-inviting a current student is a
    no-op rather than a way to reset their status.
    """
    reference = enrolments_collection().document(key_for(coach_uid, student_uid))
    existing = reference.get()

    if existing.exists:
        current = Enrolment(existing.to_dict() or {})
        if current.status == "active":
            return current

    reference.set(
        {
            "coachUid": coach_uid,
            "studentUid": student_uid,
            "status": "pending",
            "note": note,
            "invitedAt": SERVER_TIMESTAMP,
            "respondedAt": None,
        }
    )

    return Enrolment(reference.get().to_dict() or {})


def apply(coach_uid: str, student_uid: str) -> Enrolment | None:
    """Move an invitation to "waiting on the coach".

    Only from ``pending``. Re-submitting an intake form after applying is
    allowed — the answers are replaced — but it does not reset the clock or
    move a decision that has already been made.
    """
    reference = enrolments_collection().document(key_for(coach_uid, student_uid))
    snapshot = reference.get()
    if not snapshot.exists:
        return None

    current = Enrolment(snapshot.to_dict() or {})
    if current.status not in ("pending", "applied"):
        return None

    if current.status == "pending":
        reference.set({"status": "applied", "appliedAt": SERVER_TIMESTAMP}, merge=True)

    return Enrolment(reference.get().to_dict() or {})


def decide(coach_uid: str, student_uid: str, *, approve: bool) -> Enrolment | None:
    """The coach's answer to an application.

    Only an ``applied`` row can be decided. A coach cannot approve somebody
    who never applied, which is what keeps enrolment something the student
    took part in rather than something done to them.
    """
    reference = enrolments_collection().document(key_for(coach_uid, student_uid))
    snapshot = reference.get()
    if not snapshot.exists:
        return None

    current = Enrolment(snapshot.to_dict() or {})
    if current.status != "applied":
        return None

    reference.set(
        {
            "status": "active" if approve else "declined",
            "respondedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return Enrolment(reference.get().to_dict() or {})


def set_status(coach_uid: str, student_uid: str, status: EnrolmentStatus) -> None:
    """Move a row to a state something else already decided on.

    Deliberately unguarded, unlike ``apply`` and ``decide``. Those two answer
    "is this transition allowed"; this one is used by the callers that have
    already established that — approval choosing between ``documents`` and
    ``active``, and the last signature completing an enrolment.
    """
    enrolments_collection().document(key_for(coach_uid, student_uid)).set(
        {"status": status, "respondedAt": SERVER_TIMESTAMP}, merge=True
    )


def current_for_student(student_uid: str) -> Enrolment | None:
    """The one enrolment that is going somewhere, whatever stage it is at.

    ``coaches_of`` answers "who teaches me" and is active-only on purpose. This
    answers "what am I in the middle of", which a student in the middle of
    signing very much is.
    """
    for status in ("active", "documents", "applied", "pending"):
        rows = _rows("studentUid", student_uid, status)
        if rows:
            return rows[0]

    return None


def respond(coach_uid: str, student_uid: str, *, accept: bool) -> Enrolment | None:
    """Answer an invitation. Only a pending one can be answered.

    Returns None when there is nothing pending, which the route turns into a
    404 — the same answer whether the invitation never existed or was already
    settled, so this cannot be used to probe for one.
    """
    reference = enrolments_collection().document(key_for(coach_uid, student_uid))
    snapshot = reference.get()
    if not snapshot.exists:
        return None

    current = Enrolment(snapshot.to_dict() or {})
    if current.status != "pending":
        return None

    reference.set(
        {
            "status": "active" if accept else "declined",
            "respondedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return Enrolment(reference.get().to_dict() or {})


def remove(coach_uid: str, student_uid: str) -> bool:
    """End an enrolment, from either side. The grant goes with it."""
    reference = enrolments_collection().document(key_for(coach_uid, student_uid))
    if not reference.get().exists:
        return False

    reference.delete()
    return True


def _rows(field: str, uid: str, status: EnrolmentStatus) -> list[Enrolment]:
    """Two equality filters and no ordering.

    Firestore serves equality-only queries from its automatic single-field
    indexes; adding an ``order_by`` here would need a composite index deployed
    before this endpoint worked at all. Ordering is done by the caller, on a
    list that is capped anyway.
    """
    snapshot = (
        enrolments_collection()
        .where(field, "==", uid)
        .where("status", "==", status)
        .limit(MAX_ROSTER)
        .get()
    )

    return [Enrolment(doc.to_dict() or {}) for doc in snapshot]


def students_of(coach_uid: str) -> list[Enrolment]:
    return _rows("coachUid", coach_uid, "active")


def pending_for_student(student_uid: str) -> list[Enrolment]:
    return _rows("studentUid", student_uid, "pending")


def applications_for(coach_uid: str) -> list[Enrolment]:
    """Students who have answered the intake form and are waiting."""
    return _rows("coachUid", coach_uid, "applied")


def signing_for(coach_uid: str) -> list[Enrolment]:
    """Approved, and working through the documents."""
    return _rows("coachUid", coach_uid, "documents")


def open_for_student(student_uid: str) -> Enrolment | None:
    """Whatever is currently in front of this trader, if anything.

    Checked in order of how much attention it wants: something waiting on the
    coach is more informative than an invitation not yet answered, because it
    tells the student they have already done their part.
    """
    for status in ("documents", "applied", "pending"):
        rows = _rows("studentUid", student_uid, status)
        if rows:
            return rows[0]

    return None


def sent_by(coach_uid: str) -> list[Enrolment]:
    """Everything a coach has sent, settled or not, newest first."""
    snapshot = (
        enrolments_collection()
        .where("coachUid", "==", coach_uid)
        .limit(MAX_ROSTER)
        .get()
    )

    rows = [Enrolment(doc.to_dict() or {}) for doc in snapshot]
    epoch = datetime.min.replace(tzinfo=UTC)
    rows.sort(key=lambda row: row.invited_at or epoch, reverse=True)
    return rows


def coaches_of(student_uid: str) -> list[Enrolment]:
    return _rows("studentUid", student_uid, "active")
