"""Broker connection storage.

One document per account in ``broker-connections``, keyed by uid. It holds a
live credential, so two rules apply here that do not apply to the journal.

**The tokens are sealed.** Firestore encrypts at rest with Google's keys;
that does not help against a leaked service account or an over-generous rule,
which is exactly the reach that matters for something that can act on a
trading account. The tokens go through ``app.core.crypto`` first.

**The password is not here at all.** It is exchanged for a token in
``services.tradovate`` and dropped. Nothing in this module has ever seen one,
and there is no field for one to land in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.core.crypto import seal_fields, unseal_fields
from app.db.firestore import broker_doc
from app.models.schemas.broker import BrokerAccount, BrokerConnection

#: Encrypted at rest. The username is in here as well as the tokens: it is
#: half of a login somebody else's system will accept, and nothing queries on
#: it — this collection is only ever addressed by uid.
_SEALED = frozenset({"accessToken", "mdAccessToken", "username"})


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def save(
    uid: str,
    *,
    broker: str,
    environment: str,
    username: str,
    tradovate_user_id: int,
    access_token: str,
    md_access_token: str,
    expires_at: datetime,
    accounts: list[BrokerAccount],
) -> None:
    """Write the connection, replacing whatever was there.

    ``set`` without merge, deliberately: reconnecting as a different Tradovate
    user must not leave the previous user's account list sitting beside the
    new token.
    """
    document: dict[str, Any] = {
        "uid": uid,
        "broker": broker,
        "environment": environment,
        "username": username,
        "tradovateUserId": tradovate_user_id,
        "accessToken": access_token,
        "mdAccessToken": md_access_token,
        "expiresAt": expires_at,
        "accounts": [account.model_dump() for account in accounts],
        "connectedAt": SERVER_TIMESTAMP,
    }

    broker_doc(uid).set(seal_fields(document, _SEALED))


def get(uid: str) -> BrokerConnection:
    """The connection as the client is allowed to see it.

    Answers "not connected" rather than raising for an account that has never
    connected one — no connection is an ordinary state, not a missing record.
    """
    snapshot = broker_doc(uid).get()
    if not snapshot.exists:
        return BrokerConnection(connected=False)

    data = unseal_fields(snapshot.to_dict() or {}, _SEALED)

    return BrokerConnection(
        connected=True,
        broker=data.get("broker"),
        environment=data.get("environment"),
        username=str(data.get("username") or ""),
        accounts=[
            BrokerAccount(**entry)
            for entry in data.get("accounts", [])
            if isinstance(entry, dict)
        ],
        connected_at=_to_datetime(data.get("connectedAt")),
        expires_at=_to_datetime(data.get("expiresAt")),
    )


def access_token(uid: str) -> str | None:
    """The stored token, for server-side calls to Tradovate.

    Separate from ``get`` so that reading the connection for display cannot
    accidentally hand a token to something that only meant to show a status.
    The one caller that needs it has to ask for it by name.
    """
    snapshot = broker_doc(uid).get()
    if not snapshot.exists:
        return None

    data = unseal_fields(snapshot.to_dict() or {}, _SEALED)
    token = data.get("accessToken")
    return str(token) if token else None


def delete(uid: str) -> None:
    """Forget the connection entirely.

    A delete rather than a disabled flag: the point of disconnecting is that
    the credential stops being held, and a row that still holds a token while
    claiming to be off is the worst of both.
    """
    broker_doc(uid).delete()
