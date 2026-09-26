"""The community feed.

Every post here is readable by every signed-in account — that is what the feed
is. Two consequences are written into these shapes rather than left to a
reader to notice:

- **An author is four fields**, the same four contact search returns. Nothing
  about a plan, a balance or a journal reaches another account through a post.
- **A post carries no figures.** Somebody may write "up 4% this week" in their
  own words, and that is theirs to say. Nothing on a post is computed by this
  service from their journal, because a feed that published derived numbers
  would be publishing the journal.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

MAX_POST = 2000
MAX_COMMENT = 600
MAX_TAGS = 5
MAX_TAG = 24

#: Comments kept on the post itself, for the feed to render without a query
#: per post. The rest live in the subcollection.
RECENT_COMMENTS = 3


class PostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_POST)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_COMMENT)


class PostComment(BaseModel):
    id: str = ""
    author_uid: str
    author_name: str = ""
    author_photo: str = ""
    text: str
    created_at: datetime | None = None


class Post(BaseModel):
    id: str
    author_uid: str
    author_name: str = ""
    author_photo: str = ""
    text: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    like_count: int = 0
    #: Whether the caller has liked it. Per-reader, so it is computed per
    #: request rather than stored on the post.
    liked: bool = False
    comment_count: int = 0
    recent_comments: list[PostComment] = Field(default_factory=list)


class PostPage(BaseModel):
    items: list[Post]
    next_cursor: str | None = None


class LikeResult(BaseModel):
    liked: bool
    like_count: int
