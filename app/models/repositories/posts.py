"""The community feed, in Firestore.

One flat collection, ordered by ``createdAt`` and read by everyone. That makes
it the only collection here without a uid filter, which is deliberate and is
the whole difference between this and the journal — so the filter is not
missing, it is absent on purpose, and this docstring is where that is recorded.

Two denormalisations, both to keep a page of the feed to a bounded number of
reads:

- ``likeCount`` on the post, with the actual likes in a subcollection keyed by
  uid. An array of uids on the document would be simpler and would stop
  working at about forty thousand likes, which is a bad way to find out.
- ``recentComments``, the newest few inline. Without it, rendering twenty posts
  costs twenty comment queries.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import (
    SERVER_TIMESTAMP,
    DocumentSnapshot,
    Increment,
    Query,
)

from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.firestore import get_client, posts_collection
from app.models.schemas.community import (
    MAX_COMMENT,
    MAX_POST,
    MAX_TAG,
    MAX_TAGS,
    RECENT_COMMENTS,
    Post,
    PostComment,
)

MAX_PAGE_SIZE = 30
_LIKES = "likes"
_COMMENTS = "comments"


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _clean_tags(tags: list[str]) -> list[str]:
    """Lowercased, de-duplicated, stripped of the hash somebody typed."""
    seen: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lstrip("#").lower()[:MAX_TAG]
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen[:MAX_TAGS]


def _comment_of(data: dict[str, Any]) -> PostComment:
    return PostComment(
        id=str(data.get("id", "")),
        author_uid=str(data.get("authorUid", "")),
        text=str(data.get("text", ""))[:MAX_COMMENT],
        created_at=_to_datetime(data.get("createdAt")),
    )


def _to_post(snapshot: DocumentSnapshot) -> Post:
    data = snapshot.to_dict() or {}
    raw = data.get("recentComments")
    recent = (
        [_comment_of(entry) for entry in raw if isinstance(entry, dict)]
        if isinstance(raw, list)
        else []
    )

    return Post(
        id=snapshot.id,
        author_uid=str(data.get("authorUid", "")),
        text=str(data.get("text", ""))[:MAX_POST],
        tags=[str(tag) for tag in data.get("tags", []) if isinstance(tag, str)],
        created_at=_to_datetime(data.get("createdAt")),
        like_count=max(0, int(data.get("likeCount", 0) or 0)),
        comment_count=max(0, int(data.get("commentCount", 0) or 0)),
        recent_comments=recent,
    )


def _cursor_of(snapshot: DocumentSnapshot) -> str:
    return base64.urlsafe_b64encode(snapshot.id.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, binascii.Error) as exc:
        raise NotFoundError("That page cursor is not readable.") from exc


def list_posts(
    *, limit: int = 20, cursor: str | None = None
) -> tuple[list[Post], str | None]:
    """A page of the feed, newest first.

    Ordered on one field, so Firestore's automatic single-field index serves
    it and there is no composite index to deploy before the feed works.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    query = (
        posts_collection()
        .order_by("createdAt", direction=Query.DESCENDING)
        .limit(limit + 1)
    )

    if cursor:
        anchor = posts_collection().document(_decode_cursor(cursor)).get()
        if anchor.exists:
            query = query.start_after(anchor)

    snapshots = list(query.stream())
    has_more = len(snapshots) > limit
    page = snapshots[:limit]

    return [_to_post(item) for item in page], _cursor_of(page[-1]) if has_more else None


def liked_by(uid: str, post_ids: list[str]) -> set[str]:
    """Which of these posts the caller has already liked.

    One batched read for the whole page rather than a query per post.
    """
    if not post_ids:
        return set()

    references = [
        posts_collection().document(post_id).collection(_LIKES).document(uid)
        for post_id in post_ids
    ]

    # get_all does not promise order, so the post id is read back off each
    # document's path rather than from the position it came back in.
    return {
        doc.reference.parent.parent.id
        for doc in get_client().get_all(references)
        if doc.exists
    }


def create(uid: str, *, text: str, tags: list[str]) -> Post:
    reference = posts_collection().document()
    reference.set(
        {
            "authorUid": uid,
            "text": text[:MAX_POST],
            "tags": _clean_tags(tags),
            "createdAt": SERVER_TIMESTAMP,
            "likeCount": 0,
            "commentCount": 0,
            "recentComments": [],
        }
    )

    return _to_post(reference.get())


def delete(uid: str, post_id: str) -> None:
    """Only the author may delete. There is no moderator role yet."""
    reference = posts_collection().document(post_id)
    snapshot = reference.get()
    if not snapshot.exists:
        raise NotFoundError("That post is gone.")

    if str((snapshot.to_dict() or {}).get("authorUid", "")) != uid:
        raise PermissionDeniedError("That is not your post.")

    reference.delete()


def set_like(uid: str, post_id: str, *, liked: bool) -> tuple[bool, int]:
    """Like or unlike, and answer with the new count.

    The count moves by an ``Increment`` rather than a read-modify-write, so two
    people liking at once cannot lose one of the two. It only moves when the
    like document actually changed state, which is what makes liking twice a
    no-op instead of a way to inflate a post.
    """
    reference = posts_collection().document(post_id)
    snapshot = reference.get()
    if not snapshot.exists:
        raise NotFoundError("That post is gone.")

    like_doc = reference.collection(_LIKES).document(uid)
    already = like_doc.get().exists

    if liked and not already:
        like_doc.set({"uid": uid, "createdAt": SERVER_TIMESTAMP})
        reference.update({"likeCount": Increment(1)})
    elif not liked and already:
        like_doc.delete()
        reference.update({"likeCount": Increment(-1)})

    fresh = reference.get().to_dict() or {}
    return liked, max(0, int(fresh.get("likeCount", 0) or 0))


def comment(uid: str, post_id: str, *, text: str) -> PostComment:
    """Add a comment, and keep the inline copy on the post in step."""
    reference = posts_collection().document(post_id)
    if not reference.get().exists:
        raise NotFoundError("That post is gone.")

    entry = reference.collection(_COMMENTS).document()
    payload = {
        "id": entry.id,
        "authorUid": uid,
        "text": text[:MAX_COMMENT],
        "createdAt": SERVER_TIMESTAMP,
    }
    entry.set(payload)

    # Written back with a real timestamp: SERVER_TIMESTAMP inside an array is
    # stored as a sentinel rather than resolved, so the inline copy has to
    # carry a time this process supplies.
    inline = {**payload, "createdAt": datetime.now(UTC)}
    current = reference.get().to_dict() or {}
    raw = current.get("recentComments")
    kept = (
        [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    )

    reference.update(
        {
            "commentCount": Increment(1),
            "recentComments": [*kept, inline][-RECENT_COMMENTS:],
        }
    )

    return _comment_of(inline)


def comments_for(post_id: str, *, limit: int = 50) -> list[PostComment]:
    snapshot = (
        posts_collection()
        .document(post_id)
        .collection(_COMMENTS)
        .order_by("createdAt", direction=Query.ASCENDING)
        .limit(limit)
        .get()
    )

    return [_comment_of(doc.to_dict() or {}) for doc in snapshot]
