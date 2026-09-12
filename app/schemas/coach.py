"""AI coach payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Role = Literal["user", "coach"]
# Named so the service that narrows a model's answer to it can say so.
Severity = Literal["low", "medium", "high"]

# Matches the front end's limits, so a message the UI accepts is never
# rejected here and vice versa.
MAX_MESSAGE = 2_000

# What the coach itself may have said. Larger than a trader's message because
# the coach now answers at length — advice, what it buys them, and what to stop
# doing — and a stored turn has to survive being validated on the way back out.
MAX_REPLY = 8_000

#: Turns replayed to the model on any one request. The store keeps more than
#: this for the trader to scroll; only the newest reach the prompt.
HISTORY_LIMIT = 20


class CoachTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Role
    text: str = Field(min_length=1, max_length=MAX_REPLY)


#: A chart, as a data URL. Bounded well under the request-size limit, which the
#: browser meets by downscaling before it uploads — a phone screenshot is
#: several megabytes and a re-encoded copy of it is a couple of hundred
#: kilobytes with everything that matters on a chart still legible.
MAX_IMAGE = 200_000

_IMAGE_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/webp;base64,",
)


class CoachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Empty when a chart carries the question on its own.
    message: str = Field(default="", max_length=MAX_MESSAGE)
    language: str = Field(default="English", max_length=40)
    # No history field. The conversation is read from the server's own store,
    # so the client no longer supplies the one part of a coach request it could
    # previously make up — and a refresh no longer erases what was said.

    #: An optional chart to read, pasted or picked in the browser.
    image: str | None = Field(default=None, max_length=MAX_IMAGE)

    @field_validator("image")
    @classmethod
    def _is_an_image(cls, value: str | None) -> str | None:
        """Only the three formats a browser produces, and only as a data URL.

        Checked because this string is forwarded to another service. A caller
        that could put an arbitrary URL here would have the server fetch it —
        that is the whole of server-side request forgery, and the fix is to
        never accept anything but inline bytes.
        """
        if value is None or value == "":
            return None
        if not value.startswith(_IMAGE_PREFIXES):
            raise ValueError("That is not a PNG, JPEG or WebP image.")
        return value

    @model_validator(mode="after")
    def _says_something(self) -> CoachRequest:
        if not self.message and self.image is None:
            raise ValueError("Send a message, a chart, or both.")
        return self


class CoachConversation(BaseModel):
    """The stored conversation, oldest turn first."""

    turns: list[CoachTurn]


class CoachReply(BaseModel):
    reply: str
    model: str
    trade_count: int


class LeakResult(BaseModel):
    title: str
    finding: str
    cost_label: str = ""
    severity: Severity = "low"
    recommendation: str = ""


class LeakResponse(BaseModel):
    result: LeakResult | None = None
    trade_count: int
    needed: int | None = None
