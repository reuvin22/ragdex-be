"""Broker connections, and the credentials behind them.

The most sensitive documents in this database. A journal entry says what
someone traded; these say how to log in and watch them trade it. They are
sealed like everything else, and on top of that the read path is split in two:

``list_connections`` returns what the app shows, with no credential in it at
all. ``credentials_for`` returns the secret, takes a connection id, and is
called from exactly one place — the sync service. Anything that only needs to
render a list physically cannot obtain a password, because the function it
calls does not return one.

The password asked for is the broker's **investor** password, which is
read-only at the broker: it cannot place an order or move money. That is the
real protection. Encryption at rest matters, but a credential that cannot do
anything harmful is a better answer than a credential that is well hidden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from google.cloud.firestore_v1 import DocumentSnapshot, Increment

from app.core.crypto import seal_fields, unseal_fields
from app.db.firestore import connections_collection
from app.schemas.connection import (
    Connection,
    ConnectionCreate,
    ConnectionState,
    ConnectionUpdate,
    Platform,
)

#: Encrypted at rest. The password obviously; the server and login because
#: together they identify a real brokerage account, and a database dump that
#: named every account its users hold would be a disclosure on its own.
_SEALED = frozenset({"investorPassword", "server", "login", "label", "message"})

#: How many accounts one trader may connect.
#:
#: A limit rather than none, because each connected account costs real money in
#: bridge fees every month — this is the one place in the app where a user
#: action has a direct recurring cost, and an unbounded loop of them is a
#: billing incident.
MAX_CONNECTIONS = 5


class ConnectionLimit(Exception):
    """Raised when a trader is already at MAX_CONNECTIONS."""


def _to_connection(snapshot: DocumentSnapshot) -> Connection:
    data = unseal_fields(snapshot.to_dict() or {}, _SEALED)

    return Connection(
        id=snapshot.id,
        platform=cast(Platform, data.get("platform", "mt5")),
        server=data.get("server", ""),
        login=data.get("login", ""),
        label=data.get("label", ""),
        state=cast(ConnectionState, data.get("state", "pending")),
        paused=bool(data.get("paused", False)),
        message=data.get("message", ""),
        created_at=data.get("createdAt"),
        last_synced_at=data.get("lastSyncedAt"),
        trades_synced=int(data.get("tradesSynced", 0) or 0),
    )


def list_connections(uid: str) -> list[Connection]:
    """Every account this trader has connected. No credentials in the result."""
    return [
        _to_connection(snapshot)
        for snapshot in connections_collection(uid).order_by("createdAt").stream()
    ]


def get_connection(uid: str, connection_id: str) -> Connection | None:
    snapshot = connections_collection(uid).document(connection_id).get()
    return _to_connection(snapshot) if snapshot.exists else None


def create_connection(uid: str, payload: ConnectionCreate) -> Connection:
    """Store a new connection, pending until the bridge says otherwise.

    Pending rather than connected: the bridge takes up to a minute to stand an
    account up and the credentials may simply be wrong. Reporting success
    before anything has succeeded is how a trader ends up staring at an empty
    journal wondering what they did wrong.
    """
    existing = list(connections_collection(uid).limit(MAX_CONNECTIONS + 1).stream())
    if len(existing) >= MAX_CONNECTIONS:
        raise ConnectionLimit

    reference = connections_collection(uid).document()
    reference.set(
        seal_fields(
            {
                "platform": payload.platform,
                "server": payload.server,
                "login": payload.login,
                "investorPassword": payload.investor_password,
                "label": payload.label,
                "state": "pending",
                "paused": False,
                "message": "",
                "createdAt": datetime.now(UTC),
                "lastSyncedAt": None,
                "tradesSynced": 0,
                # Filled in by the sync service once the bridge has an account
                # for this. Ours is the id in the path; this is theirs.
                "bridgeAccountId": None,
                # The newest deal already imported, so a sync asks the bridge
                # for what happened since rather than re-reading years of it.
                "syncedThrough": None,
            },
            _SEALED,
        )
    )

    return _to_connection(reference.get())


def update_connection(
    uid: str, connection_id: str, payload: ConnectionUpdate
) -> Connection | None:
    """Rename or pause. Credentials are not editable — reconnect instead.

    Deliberate: a half-updated credential that fails to connect leaves an
    account in a state nobody can reason about, and "delete and add again" is
    two clicks.
    """
    reference = connections_collection(uid).document(connection_id)
    if not reference.get().exists:
        return None

    changes: dict[str, Any] = {}
    if payload.label is not None:
        changes["label"] = payload.label
    if payload.paused is not None:
        changes["paused"] = payload.paused

    if changes:
        reference.update(seal_fields(changes, _SEALED))

    return _to_connection(reference.get())


def delete_connection(uid: str, connection_id: str) -> bool:
    """Forget an account, credentials and all.

    Trades already imported are left alone. They are the trader's record of
    their own trading, not the connection's property — disconnecting a broker
    should not silently delete a year of journal.
    """
    reference = connections_collection(uid).document(connection_id)
    if not reference.get().exists:
        return False

    reference.delete()
    return True


# ----------------------------------------------------------- the sync side


def credentials_for(uid: str, connection_id: str) -> dict[str, Any] | None:
    """The secret half. Called only by the sync service.

    Separate from every other read on purpose — see the module docstring. If
    you are adding a caller for this, be sure it genuinely needs a password
    rather than a connection.
    """
    snapshot = connections_collection(uid).document(connection_id).get()
    if not snapshot.exists:
        return None

    data = unseal_fields(snapshot.to_dict() or {}, _SEALED)
    return {
        "platform": data.get("platform", "mt5"),
        "server": data.get("server", ""),
        "login": data.get("login", ""),
        "password": data.get("investorPassword", ""),
        "bridge_account_id": data.get("bridgeAccountId"),
        "synced_through": data.get("syncedThrough"),
    }


def mark_state(
    uid: str,
    connection_id: str,
    state: ConnectionState,
    *,
    message: str = "",
    bridge_account_id: str | None = None,
) -> None:
    """Record what the bridge said, including when it said no."""
    changes: dict[str, Any] = {"state": state, "message": message}
    if bridge_account_id is not None:
        changes["bridgeAccountId"] = bridge_account_id

    connections_collection(uid).document(connection_id).update(
        seal_fields(changes, _SEALED)
    )


def mark_synced(uid: str, connection_id: str, *, through: datetime, added: int) -> None:
    """Move the watermark forward after a successful pass.

    ``Increment`` rather than read-modify-write: two syncs for one trader can
    overlap, and a lost update here would under-report the count forever.
    """
    connections_collection(uid).document(connection_id).update(
        {
            "lastSyncedAt": datetime.now(UTC),
            "syncedThrough": through,
            "tradesSynced": Increment(added),
        }
    )
