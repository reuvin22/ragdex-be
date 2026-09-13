"""Upload slot payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: What the image is for, and the folder it lands in.
#:
#: The category leads the key rather than the uid. Browsing the bucket is the
#: smaller reason; the real one is that R2 lifecycle rules match on prefix, so
#: this layout can say "expire ai/ after ninety days" and leave profile photos
#: alone. A uid-first layout cannot express that at all.
UploadKind = Literal["profile", "charts", "ai", "messages"]


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
