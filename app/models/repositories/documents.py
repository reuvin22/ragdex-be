"""Coaching documents, and what students send back.

One collection, ``university-docs``, with submissions as a subcollection keyed
by the student's uid. Keying by uid rather than auto-generating gives two
things for free: a student cannot sign the same agreement twice and inflate a
count, and "has this person completed it" is a document read rather than a
query.

Markup is sanitised on the way in, the same as the invitation template — an
agreement is prose one account writes and another account's browser renders,
which is the shape of a stored-XSS bug if it is not cleaned at the boundary.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, DocumentSnapshot, Query

from app.core.errors import NotFoundError
from app.db.firestore import documents_collection, get_client
from app.models.schemas.documents import (
    MAX_ANSWER,
    SCALE_MAX,
    SCALE_MIN,
    Answer,
    DocumentKind,
    DocumentWrite,
    Question,
    Submission,
    UniversityDocument,
)
from app.views.mail_html import sanitise

_SUBMISSIONS = "submissions"

#: How many documents one coach may hold. A roster screen lists all of them.
MAX_DOCUMENTS = 60


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def digest_of(document: UniversityDocument) -> str:
    """What a signature is a signature *of*.

    Covers the title, the prose and the questions — everything a person would
    have read before agreeing. Not the publish flag or the timestamps, which
    change without changing what was agreed to.
    """
    parts = [document.title, document.body_html]
    parts.extend(
        f"{q.id}:{q.type}:{q.prompt}:{'|'.join(q.choices)}" for q in document.questions
    )

    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _question_of(raw: Any) -> Question | None:
    if not isinstance(raw, dict):
        return None

    try:
        return Question(
            id=str(raw.get("id", "")),
            type=raw.get("type", "short_text"),
            prompt=str(raw.get("prompt", "")),
            help_text=str(raw.get("helpText", "")),
            required=bool(raw.get("required", False)),
            choices=[str(c) for c in raw.get("choices", []) if isinstance(c, str)],
        )
    except ValueError:
        # A single unreadable question costs that question, not the document.
        return None


def _created(entry: UniversityDocument) -> datetime:
    """Sort key. Firestore writes the timestamp, so a document read back in the
    same breath as it was written can still be missing one."""
    return entry.created_at or datetime.min.replace(tzinfo=UTC)


def _to_document(snapshot: DocumentSnapshot) -> UniversityDocument:
    data = snapshot.to_dict() or {}
    # Narrowed to the literal before it reaches the model: a stored value that
    # is not one of the two is read as an agreement rather than refused, so a
    # bad row costs its kind and not the whole list.
    stored = str(data.get("kind", ""))
    raw_kind: DocumentKind = "form" if stored == "form" else "agreement"
    raw = data.get("questions")
    questions = (
        [q for q in (_question_of(item) for item in raw) if q is not None]
        if isinstance(raw, list)
        else []
    )

    return UniversityDocument(
        id=snapshot.id,
        coach_uid=str(data.get("coachUid", "")),
        kind=raw_kind,
        title=str(data.get("title", "")),
        summary=str(data.get("summary", "")),
        body_html=str(data.get("bodyHtml", "")),
        questions=questions,
        required=bool(data.get("required", True)),
        published=bool(data.get("published", False)),
        is_intake=bool(data.get("isIntake", False)),
        created_at=_to_datetime(data.get("createdAt")),
        updated_at=_to_datetime(data.get("updatedAt")),
    )


def _payload(
    coach_uid: str, payload: DocumentWrite, *, existing: list[Question]
) -> dict[str, Any]:
    """The stored shape, with question ids minted and markup cleaned.

    Ids already in use are kept. That is what makes editing a form safe: the
    answers already collected are keyed by question id, and regenerating them
    on every save would orphan every one.
    """
    known = {question.id for question in existing if question.id}
    questions: list[dict[str, Any]] = []

    for question in payload.questions:
        identifier = question.id if question.id in known else uuid.uuid4().hex[:12]
        questions.append(
            {
                "id": identifier,
                "type": question.type,
                "prompt": question.prompt.strip(),
                "helpText": question.help_text.strip(),
                "required": question.required,
                "choices": [c.strip() for c in question.choices if c.strip()],
            }
        )

    return {
        "coachUid": coach_uid,
        "kind": payload.kind,
        "title": payload.title.strip(),
        "summary": payload.summary.strip(),
        "bodyHtml": sanitise(payload.body_html),
        "questions": questions,
        "required": payload.required,
        "published": payload.published,
        "isIntake": payload.is_intake,
        "updatedAt": SERVER_TIMESTAMP,
    }


def create(coach_uid: str, payload: DocumentWrite) -> UniversityDocument:
    reference = documents_collection().document()
    reference.set(
        {**_payload(coach_uid, payload, existing=[]), "createdAt": SERVER_TIMESTAMP}
    )
    return _to_document(reference.get())


def get(document_id: str) -> UniversityDocument:
    snapshot = documents_collection().document(document_id).get()
    if not snapshot.exists:
        raise NotFoundError("No such document.")
    return _to_document(snapshot)


def update(
    coach_uid: str, document_id: str, payload: DocumentWrite
) -> UniversityDocument:
    current = get(document_id)
    reference = documents_collection().document(document_id)
    reference.set(_payload(coach_uid, payload, existing=current.questions), merge=True)
    return _to_document(reference.get())


def delete(document_id: str) -> None:
    """Remove the document and everything sent back to it.

    Firestore does not cascade, so a delete that left the subcollection behind
    would leave signatures pointing at a document nobody can read — the worst
    of both: still stored, no longer evidence.
    """
    reference = documents_collection().document(document_id)

    for entry in reference.collection(_SUBMISSIONS).limit(500).stream():
        entry.reference.delete()

    reference.delete()


def for_coach(coach_uid: str) -> list[UniversityDocument]:
    """Everything this coach has made, drafts included."""
    snapshot = (
        documents_collection()
        .where("coachUid", "==", coach_uid)
        .limit(MAX_DOCUMENTS)
        .get()
    )

    documents = [_to_document(doc) for doc in snapshot]
    documents.sort(key=_created, reverse=True)
    return documents


def published_for(coach_uid: str) -> list[UniversityDocument]:
    """What a student may see: this coach's published documents only.

    Two equality filters and no ordering, so Firestore's automatic indexes
    serve it. A draft is not merely hidden in the client — it never leaves
    here.
    """
    snapshot = (
        documents_collection()
        .where("coachUid", "==", coach_uid)
        .where("published", "==", True)
        .limit(MAX_DOCUMENTS)
        .get()
    )

    documents = [_to_document(doc) for doc in snapshot]
    documents.sort(key=_created, reverse=True)
    return documents


# ----------------------------------------------------------- submissions


def _answer_of(raw: Any) -> Answer | None:
    if not isinstance(raw, dict):
        return None

    return Answer(
        question_id=str(raw.get("questionId", ""))[:64] or "unknown",
        value=str(raw.get("value", ""))[:MAX_ANSWER],
        values=[str(v)[:MAX_ANSWER] for v in raw.get("values", []) if isinstance(v, str)],
    )


def _to_submission(document_id: str, snapshot: DocumentSnapshot) -> Submission:
    data = snapshot.to_dict() or {}
    raw = data.get("answers")
    answers = (
        [a for a in (_answer_of(item) for item in raw) if a is not None]
        if isinstance(raw, list)
        else []
    )

    return Submission(
        document_id=document_id,
        student_uid=snapshot.id,
        signed_name=str(data.get("signedName", "")),
        answers=answers,
        submitted_at=_to_datetime(data.get("submittedAt")),
        document_digest=str(data.get("documentDigest", "")),
    )


def clamp_answers(document: UniversityDocument, answers: list[Answer]) -> list[Answer]:
    """Keep only answers to questions this document actually asks.

    A submission naming a question id that is not on the form is either a
    stale client or someone probing; either way it is not an answer to
    anything, and storing it would put unreviewed strings in a coach's export.
    """
    by_id = {question.id: question for question in document.questions}
    kept: list[Answer] = []

    for answer in answers:
        question = by_id.get(answer.question_id)
        if question is None:
            continue

        if question.type == "multi_choice":
            allowed = set(question.choices)
            kept.append(
                Answer(
                    question_id=answer.question_id,
                    values=[v for v in answer.values if v in allowed],
                )
            )
        elif question.type == "single_choice":
            value = answer.value if answer.value in question.choices else ""
            kept.append(Answer(question_id=answer.question_id, value=value))
        elif question.type == "scale":
            # Clamped rather than refused: a slider that sent 7 is a bug in a
            # client, and losing the whole submission over it helps nobody.
            try:
                number = int(answer.value)
            except ValueError:
                number = SCALE_MIN
            bounded = max(SCALE_MIN, min(SCALE_MAX, number))
            kept.append(Answer(question_id=answer.question_id, value=str(bounded)))
        else:
            kept.append(
                Answer(question_id=answer.question_id, value=answer.value[:MAX_ANSWER])
            )

    return kept


def submit(
    document: UniversityDocument,
    student_uid: str,
    *,
    signed_name: str,
    answers: list[Answer],
) -> Submission:
    """Record what a student sent.

    Keyed by uid, so submitting twice replaces rather than accumulates — and
    the digest is taken now, from the document as it stands, which is what
    makes the record evidence of something specific.
    """
    reference = (
        documents_collection()
        .document(document.id)
        .collection(_SUBMISSIONS)
        .document(student_uid)
    )

    reference.set(
        {
            "studentUid": student_uid,
            "signedName": signed_name.strip()[:200],
            "answers": [
                {
                    "questionId": answer.question_id,
                    "value": answer.value,
                    "values": answer.values,
                }
                for answer in clamp_answers(document, answers)
            ],
            "submittedAt": SERVER_TIMESTAMP,
            "documentDigest": digest_of(document),
        }
    )

    return _to_submission(document.id, reference.get())


def submission_for(document_id: str, student_uid: str) -> Submission | None:
    snapshot = (
        documents_collection()
        .document(document_id)
        .collection(_SUBMISSIONS)
        .document(student_uid)
        .get()
    )

    return _to_submission(document_id, snapshot) if snapshot.exists else None


def submissions_for(document_id: str) -> list[Submission]:
    snapshot = (
        documents_collection()
        .document(document_id)
        .collection(_SUBMISSIONS)
        .order_by("submittedAt", direction=Query.DESCENDING)
        .limit(MAX_DOCUMENTS * 10)
        .get()
    )

    return [_to_submission(document_id, doc) for doc in snapshot]


def submitted_uids(document_id: str) -> set[str]:
    """Who has completed it, as ids only — one read per document."""
    snapshot = (
        documents_collection()
        .document(document_id)
        .collection(_SUBMISSIONS)
        .select([])
        .limit(MAX_DOCUMENTS * 10)
        .get()
    )

    return {doc.id for doc in snapshot}


def intake_for(coach_uid: str) -> UniversityDocument | None:
    """The one form an invited trader fills in, if this coach set one."""
    snapshot = (
        documents_collection()
        .where("coachUid", "==", coach_uid)
        .where("isIntake", "==", True)
        .limit(1)
        .get()
    )

    for doc in snapshot:
        document = _to_document(doc)
        return document if document.published else None

    return None


def clear_intake(coach_uid: str, *, except_id: str) -> None:
    """Keep "the intake form" naming one thing.

    Called after a document is marked as the intake form. A coach who flags a
    second one has changed their mind, not created an ambiguity — so the first
    is unflagged rather than the second being refused.
    """
    snapshot = (
        documents_collection()
        .where("coachUid", "==", coach_uid)
        .where("isIntake", "==", True)
        .limit(MAX_DOCUMENTS)
        .get()
    )

    for doc in snapshot:
        if doc.id != except_id:
            doc.reference.set({"isIntake": False}, merge=True)


def required_documents(coach_uid: str) -> list[UniversityDocument]:
    """What a student has to complete, in the order they will be asked.

    Published and required: a draft cannot block somebody, and an optional
    document is optional — neither belongs in a gate.

    **Forms before agreements.** A program asks about you, then asks you to
    agree to something; doing that the other way round means signing terms
    before saying who you are, and the answers are often what the terms are
    about. Within each kind, oldest first — the order the coach built them,
    which is the order they were thinking in.
    """
    documents = [
        document for document in published_for(coach_uid) if document.required
    ]

    forms = sorted(
        (d for d in documents if d.kind == "form"), key=_created
    )
    agreements = sorted(
        (d for d in documents if d.kind != "form"), key=_created
    )

    return [*forms, *agreements]


def required_ids(coach_uid: str) -> list[str]:
    return [document.id for document in required_documents(coach_uid)]


def unsigned_ids(student_uid: str, document_ids: list[str]) -> list[str]:
    """Which of these the student has not completed.

    One batched read rather than a query per document: submissions are keyed
    by uid, so the whole answer is a ``get_all`` over known paths.
    """
    if not document_ids:
        return []

    references = [
        documents_collection().document(document_id).collection(_SUBMISSIONS).document(student_uid)
        for document_id in document_ids
    ]

    signed = {
        doc.reference.parent.parent.id
        for doc in get_client().get_all(references)
        if doc.exists
    }

    return [document_id for document_id in document_ids if document_id not in signed]
