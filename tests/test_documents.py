"""Who can see, edit and submit a coaching document.

The boundary is the point of this family: a document belongs to a coach, is
readable by their students once published, and is invisible to everyone else.
Each test below is a way that could leak rather than a feature working.
"""

from __future__ import annotations

import pytest
from app.models.repositories.documents import clamp_answers, digest_of
from app.models.schemas.documents import (
    Answer,
    Question,
    Submission,
    SubmissionWrite,
    UniversityDocument,
)
from app.models.schemas.profile import Profile
from app.services.university import check_complete

DOC = "doc-1"
OTHER_COACH = "coach-9"


def _document(**over) -> UniversityDocument:
    base = {
        "id": DOC,
        "coach_uid": OTHER_COACH,
        "kind": "agreement",
        "title": "Code of conduct",
        "body_html": "<p>Be careful.</p>",
        "published": True,
    }
    return UniversityDocument(**{**base, **over})


@pytest.fixture
def coach_profile(monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.profiles_repo.get_profile",
        lambda uid: Profile(uid=uid, email="c@example.com", account_type="coach"),
    )


def _serve(monkeypatch, document: UniversityDocument) -> None:
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.get", lambda _id: document
    )
    # Signing the last outstanding document enrols somebody. Not what these
    # tests are about, and it would reach Firestore.
    monkeypatch.setattr(
        "app.controllers.v1.university.service.complete_if_signed",
        lambda coach, student: False,
    )


class _Row:
    """Just enough of an enrolment for the read rule to judge."""

    def __init__(self, status: str) -> None:
        self.status = status


def _enrolled(monkeypatch, active: bool, *, status: str | None = None) -> None:
    """Put an enrolment — or none — behind the caller.

    The read rule reads the row rather than asking "is it active", because it
    has to tell `active` from `documents` from `pending`. Tests say which.
    """
    settled = status or ("active" if active else "declined")
    row = _Row(settled) if (active or status) else None

    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.get", lambda coach, student: row
    )
    monkeypatch.setattr(
        "app.controllers.v1.university.enrolment_repo.is_active",
        lambda coach, student: active,
    )


# ------------------------------------------------------------------ reading


def test_a_stranger_cannot_read_a_document(client, monkeypatch):
    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, False)

    # 404 and not 403: "forbidden" would confirm the document exists.
    assert client.get(f"/api/v1/university/documents/{DOC}").status_code == 404


def test_a_draft_is_invisible_even_to_an_enrolled_student(client, monkeypatch):
    _serve(monkeypatch, _document(published=False))
    _enrolled(monkeypatch, True)

    assert client.get(f"/api/v1/university/documents/{DOC}").status_code == 404


def test_an_enrolled_student_reads_a_published_document(client, monkeypatch):
    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, True)
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.submission_for",
        lambda _doc, _uid: None,
    )

    response = client.get(f"/api/v1/university/documents/{DOC}")

    assert response.status_code == 200
    # A student is never told how many other people have completed it.
    assert response.json()["submission_count"] is None


def test_the_author_reads_their_own_draft(client, monkeypatch):
    _serve(monkeypatch, _document(coach_uid="trader-1", published=False))
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.submitted_uids",
        lambda _doc: {"a", "b"},
    )

    response = client.get(f"/api/v1/university/documents/{DOC}")

    assert response.status_code == 200
    assert response.json()["submission_count"] == 2


# ------------------------------------------------------------------ editing


def test_you_cannot_edit_somebody_elses_document(client, monkeypatch, coach_profile):
    _serve(monkeypatch, _document())

    response = client.put(
        f"/api/v1/university/documents/{DOC}",
        json={"kind": "agreement", "title": "Mine now"},
    )

    assert response.status_code == 404


def test_you_cannot_delete_somebody_elses_document(client, monkeypatch):
    _serve(monkeypatch, _document())

    assert client.delete(f"/api/v1/university/documents/{DOC}").status_code == 404


def test_only_a_coach_account_may_create_one(client, monkeypatch):
    monkeypatch.setattr(
        "app.controllers.v1.university.profiles_repo.get_profile",
        lambda uid: Profile(uid=uid, email="s@example.com", account_type="student"),
    )

    response = client.post(
        "/api/v1/university/documents", json={"kind": "form", "title": "Q"}
    )

    assert response.status_code == 403


def test_a_document_refuses_an_unexpected_field(client, coach_profile):
    response = client.post(
        "/api/v1/university/documents",
        json={"kind": "form", "title": "Q", "coach_uid": "somebody-else"},
    )

    assert response.status_code == 422


# --------------------------------------------------------------- submitting


def test_a_stranger_cannot_submit(client, monkeypatch):
    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, False)

    response = client.post(
        f"/api/v1/university/documents/{DOC}/submit", json={"signed_name": "Me"}
    )

    assert response.status_code == 404


def test_an_agreement_needs_a_signature(client, monkeypatch):
    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, True)

    response = client.post(
        f"/api/v1/university/documents/{DOC}/submit", json={"signed_name": "   "}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsigned"


def test_a_form_refuses_a_missing_required_answer(client, monkeypatch):
    _serve(
        monkeypatch,
        _document(
            kind="form",
            questions=[
                Question(id="q1", type="short_text", prompt="Why?", required=True)
            ],
        ),
    )
    _enrolled(monkeypatch, True)

    response = client.post(
        f"/api/v1/university/documents/{DOC}/submit", json={"answers": []}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "incomplete"


def test_a_submission_records_the_session_uid(client, monkeypatch):
    """The whole reason a signature is worth keeping.

    Nothing in the body names the author — if it could, the record would be a
    claim about who signed rather than evidence of it.
    """
    seen: list[str] = []

    def _submit(document, uid, *, signed_name, answers):
        seen.append(uid)
        return Submission(
            document_id=document.id, student_uid=uid, signed_name=signed_name
        )

    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, True)
    monkeypatch.setattr(
        "app.controllers.v1.university.documents_repo.submit", _submit
    )

    response = client.post(
        f"/api/v1/university/documents/{DOC}/submit", json={"signed_name": "Alex"}
    )

    assert response.status_code == 201
    assert seen == ["trader-1"]


def test_a_coach_cannot_submit_to_their_own_document(client, monkeypatch):
    _serve(monkeypatch, _document(coach_uid="trader-1"))

    response = client.post(
        f"/api/v1/university/documents/{DOC}/submit", json={"signed_name": "Me"}
    )

    assert response.status_code == 400


def test_a_student_cannot_read_the_submissions_list(client, monkeypatch):
    _serve(monkeypatch, _document())
    _enrolled(monkeypatch, True)

    response = client.get(f"/api/v1/university/documents/{DOC}/submissions")

    assert response.status_code == 404


# ------------------------------------------------------- answers are bounded


def test_an_answer_to_a_question_that_does_not_exist_is_dropped():
    document = _document(
        kind="form", questions=[Question(id="q1", type="short_text", prompt="Why?")]
    )

    kept = clamp_answers(
        document,
        [Answer(question_id="q1", value="ok"), Answer(question_id="ghost", value="x")],
    )

    assert [answer.question_id for answer in kept] == ["q1"]


def test_a_choice_that_was_not_offered_is_dropped():
    document = _document(
        kind="form",
        questions=[
            Question(id="q1", type="single_choice", prompt="Pick", choices=["a", "b"])
        ],
    )

    assert clamp_answers(document, [Answer(question_id="q1", value="c")])[0].value == ""


def test_a_scale_answer_is_clamped_rather_than_refused():
    document = _document(
        kind="form", questions=[Question(id="q1", type="scale", prompt="How sure?")]
    )

    def answer(value: str) -> str:
        return clamp_answers(document, [Answer(question_id="q1", value=value)])[0].value

    assert answer("99") == "5"
    assert answer("-3") == "1"
    assert answer("nope") == "1"


def test_the_digest_changes_when_the_text_does():
    before = digest_of(_document(body_html="<p>Be careful.</p>"))
    after = digest_of(_document(body_html="<p>Be careless.</p>"))

    assert before != after


def test_the_digest_ignores_things_nobody_agreed_to():
    assert digest_of(_document(published=True)) == digest_of(_document(published=False))


def test_check_complete_accepts_a_filled_form():
    document = _document(
        kind="form",
        questions=[Question(id="q1", type="short_text", prompt="Why?", required=True)],
    )

    check_complete(
        document, SubmissionWrite(answers=[Answer(question_id="q1", value="x")])
    )
