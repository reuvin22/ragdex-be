"""Choosing a bridge, and what happens when there is not one.

Separate from ``bridge.py`` — that file is the interface, this one is the
wiring. Keeping them apart is what lets the sync service be tested against a
fake without ever importing a vendor's URL.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.bridge import Bridge, BridgeError
from app.services.metaapi import MetaApiBridge
from app.services.mt5bridge import Mt5BridgeAdapter


class UnconfiguredBridge:
    """The bridge when nobody has configured one.

    Refuses loudly rather than doing nothing, and that is the whole point of
    the class. Without it a connection sits on "Connecting…" for ever: the
    trader thinks their broker rejected them, the operator sees no error
    because nothing ran, and the only way to find out is to read the code.

    An honest failure is a better product than a silent one.
    """

    def connect(self, credentials: object) -> str:
        raise BridgeError(
            "Broker sync is not switched on yet. Your account is saved — it "
            "will start importing as soon as it is enabled.",
            status_code=501,
        )

    def closed_trades(self, account_id: str, since: object) -> list:
        raise BridgeError("Broker sync is not switched on yet.", status_code=501)

    def disconnect(self, account_id: str) -> None:
        # Nothing was ever stood up, so there is nothing to release. Silent on
        # purpose: disconnecting an account must always work, even when the
        # thing it was connected to never existed.
        return None


def bridge_for(settings: Settings) -> Bridge:
    """The bridge this deployment is configured to use.

    A function rather than a module-level instance, so settings are read at
    call time and a test can hand in its own.
    """
    if not settings.bridge_configured:
        return UnconfiguredBridge()  # type: ignore[return-value]

    if settings.bridge_provider == "metaapi":
        return MetaApiBridge(settings)

    if settings.bridge_provider == "mt5bridge":
        return Mt5BridgeAdapter(settings)

    raise BridgeError(
        f"Unknown broker sync provider: {settings.bridge_provider!r}.",
        status_code=500,
    )
