"""The coach-student grant.

This family is the first place one account reads another's data, so these
tests are about the boundary rather than the feature. The question each one
asks is the same: can a caller reach a journal they were not given?
"""

from __future__ import annotations

import pytest
from app.models.schemas.directory import DirectoryEntry
from app.models.schemas.profile import Profile

STUDENT = "student-9"


@pytest.fixture
def coach_profile(monkeypatch):
    """The caller is a Coach account, which is what holding a roster needs."""

    def _profile(uid: str) -> Profile:
        return Profile(uid=uid, email="coach@example.com", account_type="coach")

    monkeypatch.setattr(
        "app.controllers.v1.university.profiles_repo.get_profile", _profile
    )


@pytest.fixture
def individual_profile(monkeypatch):
    def _profile(uid: str) -> Profile:
        return Profile(uid=uid, email="solo@example.com", account_type="individual")

    monkeypatch.setattr(
        "app.controllers.v1.university.profiles_repo.get_profile", _profile
    )


def _grant(monkeypatch, *, active: bool) -> list[tuple[str, str]]:
    """Record every permission check, so a test can prove one happened."""
    asked: list[tuple[str, str]] = []

    def _is_active(coach_uid: str, student_uid: str) -> bool:
        asked.append((coach_uid, student_uid))
        return active

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active", _is_active
    )
    return asked


# ---------------------------------------------------------------- the grant


def test_a_stranger_cannot_read_a_journal(client, monkeypatch):
    """The whole point. No enrolment, no trades."""
    asked = _grant(monkeypatch, active=False)

    def _boom(*args, **kwargs):
        raise AssertionError("the journal was read without a grant")

    monkeypatch.setattr(
        "app.controllers.v1.university.trades_repo.list_trades", _boom
    )

    response = client.get(f"/api/v1/university/students/{STUDENT}/journal")

    assert response.status_code == 403
    assert asked == [("trader-1", STUDENT)]


def test_the_grant_is_checked_before_the_journal_is_touched(client, monkeypatch):
    """Ordering matters: a read that happens and is then discarded has still
    happened, and on a shared database that is the part that costs."""
    order: list[str] = []

    def _is_active(coach_uid: str, student_uid: str) -> bool:
        order.append("checked")
        return True

    def _list(uid: str, **kwargs):
        order.append("read")
        return [], None

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active", _is_active
    )
    monkeypatch.setattr("app.controllers.v1.university.trades_repo.list_trades", _list)

    assert client.get(f"/api/v1/university/students/{STUDENT}/journal").status_code == 200
    assert order == ["checked", "read"]


def test_a_coach_reads_the_student_they_were_given(client, monkeypatch):
    asked = _grant(monkeypatch, active=True)
    monkeypatch.setattr(
        "app.controllers.v1.university.trades_repo.list_trades",
        lambda uid, **kwargs: ([], None),
    )

    response = client.get(f"/api/v1/university/students/{STUDENT}/journal")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    # Asked about *that* student, on behalf of *this* caller — never the
    # other way round, which would hand a student their coach's journal.
    assert asked == [("trader-1", STUDENT)]


def test_the_grant_is_directional(client, monkeypatch):
    """`is_active(caller, other)` and not `is_active(other, caller)`.

    Reversing these arguments is the one mistake in this file that would still
    pass every other test here, because both orders are two uids.
    """
    asked = _grant(monkeypatch, active=True)
    monkeypatch.setattr(
        "app.controllers.v1.university.trades_repo.list_trades",
        lambda uid, **kwargs: ([], None),
    )

    client.get(f"/api/v1/university/students/{STUDENT}/journal")

    coach_uid, student_uid = asked[0]
    assert coach_uid == "trader-1"
    assert student_uid == STUDENT


# --------------------------------------------------------------- invitations


def test_only_a_coach_account_may_invite(client, individual_profile):
    response = client.post(
        "/api/v1/university/invites", json={"email": "someone@example.com"}
    )

    assert response.status_code == 403
    assert "Coach" in response.json()["error"]["message"]


def test_inviting_an_unknown_address_is_a_404(client, coach_profile, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.directory_repo.find_by_email",
        lambda email: None,
    )

    response = client.post(
        "/api/v1/university/invites", json={"email": "nobody@example.com"}
    )

    assert response.status_code == 404


def test_a_coach_cannot_invite_themselves(client, coach_profile, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.directory_repo.find_by_email",
        lambda email: DirectoryEntry(uid="trader-1", email=email),
    )

    response = client.post(
        "/api/v1/university/invites", json={"email": "coach@example.com"}
    )

    assert response.status_code == 400


def test_an_invite_stores_the_pair_and_nothing_the_caller_sent(
    client, coach_profile, monkeypatch
):
    """The invite names a uid the *service* resolved, not one the client sent."""
    written: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        "app.controllers.v1.university.directory_repo.find_by_email",
        lambda email: DirectoryEntry(uid=STUDENT, email="s@example.com"),
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.invite",
        lambda coach, student, *, note: written.append((coach, student, note)),
    )

    response = client.post(
        "/api/v1/university/invites",
        json={"email": "s@example.com", "note": "Come and join."},
    )

    assert response.status_code == 201
    assert written == [("trader-1", STUDENT, "Come and join.")]


def test_an_invite_refuses_an_unexpected_field(client, coach_profile):
    """Every schema is extra="forbid"; a uid smuggled in a body is refused."""
    response = client.post(
        "/api/v1/university/invites",
        json={"email": "s@example.com", "student_uid": "somebody-else"},
    )

    assert response.status_code == 422


def test_accepting_answers_only_your_own_invitation(client, monkeypatch):
    """The row is addressed <coach>_<caller>, so the caller is always the
    student half — there is no id here that could name somebody else's."""
    answered: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.coaches_of", lambda uid: []
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.respond",
        lambda coach, student, *, accept: (
            answered.append((coach, student, accept)) or object()
        ),
    )

    response = client.post("/api/v1/university/invitations/coach-7/accept")

    assert response.status_code == 200
    assert answered == [("coach-7", "trader-1", True)]


def test_you_cannot_hold_two_coaches(client, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.coaches_of",
        lambda uid: [object()],
    )

    response = client.post("/api/v1/university/invitations/coach-7/accept")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "already_enrolled"


def test_answering_a_settled_invitation_is_a_404(client, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.coaches_of", lambda uid: []
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.respond",
        lambda coach, student, *, accept: None,
    )

    assert (
        client.post("/api/v1/university/invitations/coach-7/decline").status_code == 404
    )


def test_a_uid_shaped_like_a_path_is_refused(client, monkeypatch):
    """The path pattern stops anything odd reaching the client library."""
    response = client.post("/api/v1/university/invitations/..%2Fadmin/accept")
    assert response.status_code in (404, 422)


# ------------------------------------------------------- the joining flow


def test_the_intake_form_is_readable_before_enrolment(client, monkeypatch):
    """The one document a non-student may read — that is its whole job."""
    from app.models.schemas.documents import UniversityDocument

    document = UniversityDocument(
        id="d1", coach_uid="coach-7", kind="form", title="Intake",
        published=True, is_intake=True,
    )

    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.get", lambda _id: document
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active",
        lambda coach, student: False,
    )

    class _Row:
        status = "pending"

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.get",
        lambda coach, student: _Row(),
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.submission_for",
        lambda _doc, _uid: None,
    )

    assert client.get("/api/v1/university/documents/d1").status_code == 200


def test_a_non_intake_form_is_not_readable_before_enrolment(client, monkeypatch):
    """The exception is the intake flag, not "any published document"."""
    from app.models.schemas.documents import UniversityDocument

    document = UniversityDocument(
        id="d1", coach_uid="coach-7", kind="form", title="Weekly review",
        published=True, is_intake=False,
    )

    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.get", lambda _id: document
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active",
        lambda coach, student: False,
    )

    assert client.get("/api/v1/university/documents/d1").status_code == 404


def test_the_intake_form_is_still_hidden_from_somebody_with_no_invitation(
    client, monkeypatch
):
    from app.models.schemas.documents import UniversityDocument

    document = UniversityDocument(
        id="d1", coach_uid="coach-7", kind="form", title="Intake",
        published=True, is_intake=True,
    )

    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.get", lambda _id: document
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active",
        lambda coach, student: False,
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.get",
        lambda coach, student: None,
    )

    assert client.get("/api/v1/university/documents/d1").status_code == 404


def test_only_a_coach_may_approve(client, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.profiles_repo.get_profile",
        lambda uid: Profile(uid=uid, email="s@example.com", account_type="student"),
    )

    assert (
        client.post("/api/v1/university/applications/student-9/approve").status_code
        == 403
    )


def test_approving_somebody_who_never_applied_is_a_404(
    client, monkeypatch, coach_profile
):
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.decide",
        lambda coach, student, *, approve: None,
    )

    assert (
        client.post("/api/v1/university/applications/student-9/approve").status_code
        == 404
    )


def test_approval_names_the_student_and_the_calling_coach(
    client, monkeypatch, coach_profile
):
    decided: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.decide",
        lambda coach, student, *, approve: (
            decided.append((coach, student, approve)) or object()
        ),
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.for_coach", lambda _uid: []
    )

    response = client.post("/api/v1/university/applications/student-9/approve")

    assert response.status_code == 200
    assert decided == [("trader-1", "student-9", True)]
