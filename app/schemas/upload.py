"""Upload slot payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: What the image is for. Only the first segment of a key, but it is what keeps
#: a chat attachment and a coach chart from landing in the same place.
UploadKind = Literal["chat", "chart", "trade"]


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: UploadKind
    #: Declared up front so the signature can bind it. The caller cannot then
    #: send something else — the signed URL only accepts what it was cut for.
    content_type: str = Field(min_length=1, max_length=100)
    content_length: int = Field(gt=0)


class UploadSlot(BaseModel):
    """Where to put one image, and what to call it afterwards."""

    key: str
    url: str
    #: Seconds. The client should upload well inside this and not cache the URL.
    expires_in: int


class ReadUrl(BaseModel):
    url: str
    expires_in: int
