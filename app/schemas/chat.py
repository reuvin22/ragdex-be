"""Chat payloads.

Only what the API still serves. Messages, contacts and read receipts live in
the Realtime Database and never pass through here — see the module docstring
in app/api/v1/routes/chat.py.
"""

from __future__ import annotations

from pydantic import BaseModel


class ChatToken(BaseModel):
    """A short-lived Firebase custom token, for the live chat connection only.

    Exchanged once by the browser for a Realtime Database session. What it can
    reach is decided by database.rules.json, not by this shape.
    """

    token: str
