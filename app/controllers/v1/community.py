"""The community feed.

The one family in this service that reads across accounts without a grant, and
the reason is that a post is published — somebody wrote it to be read. That is
a real departure from everything else here, so the boundary is stated rather
than assumed:

- **A feed read returns posts and authors, never accounts.** An author is the
  four directory fields, the same ones contact search returns. No plan, no
  balance, no journal, no account type.
- **Nothing is derived from a journal.** A trader may write what they like
  about their own week; this service does not compute a figure and attach it.
- **Writes are the caller's own.** Author is the session uid, likes are keyed
  by it, and a delete checks it. No route accepts an author.

It does mean the feed shows that an account exists to anyone who reads it —
which contact search deliberately avoids by requiring an address. That is
inherent to a public feed rather than an oversight: posting is the act of
being visible.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query, Response, status

from app.controllers.deps import ReadUser, StandardRateLimit, WriteUser
from app.models.repositories import directory as directory_repo
from app.models.repositories import posts as posts_repo
from app.models.schemas.common import ErrorResponse
from app.models.schemas.community import (
    CommentCreate,
    LikeResult,
    Post,
    PostComment,
    PostCreate,
    PostPage,
)

router = APIRouter(
    prefix="/community",
    tags=["community"],
    dependencies=[StandardRateLimit],
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
    },
)

PostId = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _with_authors(items: list[Post], *, viewer: str) -> list[Post]:
    """Fill in who wrote each post and each inline comment.

    One directory read for every uid on the page, resolved together — a lookup
    per post would turn a feed of twenty into forty round trips.
    """
    uids = {post.author_uid for post in items}
    for post in items:
        uids.update(comment.author_uid for comment in post.recent_comments)

    people = directory_repo.get_many([uid for uid in uids if uid])
    liked = posts_repo.liked_by(viewer, [post.id for post in items])

    for post in items:
        author = people.get(post.author_uid)
        if author is not None:
            post.author_name = author.display_name
            post.author_photo = author.photo_url

        post.liked = post.id in liked

        for entry in post.recent_comments:
            who = people.get(entry.author_uid)
            if who is not None:
                entry.author_name = who.display_name
                entry.author_photo = who.photo_url

    return items


@router.get("/posts", response_model=PostPage, summary="The feed")
async def list_posts(
    user: ReadUser,
    limit: int = Query(default=20, ge=1, le=posts_repo.MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=256),
) -> PostPage:
    """Newest first, cursor-paged, every author."""
    items, next_cursor = posts_repo.list_posts(limit=limit, cursor=cursor)
    return PostPage(items=_with_authors(items, viewer=user.uid), next_cursor=next_cursor)


@router.post(
    "/posts",
    response_model=Post,
    status_code=status.HTTP_201_CREATED,
    summary="Write a post",
    responses={403: {"model": ErrorResponse, "description": "Email not confirmed"}},
)
async def create_post(user: WriteUser, payload: PostCreate) -> Post:
    """The author is the session, never the body."""
    post = posts_repo.create(user.uid, text=payload.text, tags=payload.tags)
    return _with_authors([post], viewer=user.uid)[0]


@router.delete(
    "/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your post",
    responses={403: {"model": ErrorResponse, "description": "Not your post"}},
)
async def delete_post(user: WriteUser, post_id: str = PostId) -> Response:
    posts_repo.delete(user.uid, post_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/posts/{post_id}/like",
    response_model=LikeResult,
    summary="Like a post",
    responses={404: {"model": ErrorResponse}},
)
async def like(user: WriteUser, post_id: str = PostId) -> LikeResult:
    """Idempotent: liking twice leaves the count where it was."""
    liked, count = posts_repo.set_like(user.uid, post_id, liked=True)
    return LikeResult(liked=liked, like_count=count)


@router.delete(
    "/posts/{post_id}/like",
    response_model=LikeResult,
    summary="Take a like back",
    responses={404: {"model": ErrorResponse}},
)
async def unlike(user: WriteUser, post_id: str = PostId) -> LikeResult:
    liked, count = posts_repo.set_like(user.uid, post_id, liked=False)
    return LikeResult(liked=liked, like_count=count)


@router.get(
    "/posts/{post_id}/comments",
    response_model=list[PostComment],
    summary="Every comment on a post",
)
async def list_comments(user: ReadUser, post_id: str = PostId) -> list[PostComment]:
    entries = posts_repo.comments_for(post_id)
    people = directory_repo.get_many([entry.author_uid for entry in entries])

    for entry in entries:
        who = people.get(entry.author_uid)
        if who is not None:
            entry.author_name = who.display_name
            entry.author_photo = who.photo_url

    return entries


@router.post(
    "/posts/{post_id}/comments",
    response_model=PostComment,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a post",
    responses={404: {"model": ErrorResponse}},
)
async def add_comment(
    user: WriteUser, payload: CommentCreate, post_id: str = PostId
) -> PostComment:
    entry = posts_repo.comment(user.uid, post_id, text=payload.text)

    who = directory_repo.get_many([user.uid]).get(user.uid)
    if who is not None:
        entry.author_name = who.display_name
        entry.author_photo = who.photo_url

    return entry
