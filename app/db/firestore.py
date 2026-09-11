"""Firebase Admin, initialised once.

The Admin SDK bypasses Firestore security rules entirely, which is precisely
why every read and write in ``app/repositories`` is scoped to a verified uid
by hand. Losing that discipline here means losing tenant isolation.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import Client

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_USERS = "users"


def init_firebase() -> None:
    """Start the Admin SDK. Safe to call more than once."""
    if firebase_admin._apps:
        return

    settings = get_settings()
    info = settings.service_account_info()

    if info is None:
        # Application Default Credentials: the right path on Cloud Run or GKE,
        # where a service account is attached to the workload instead.
        logger.info("No service account configured; using default credentials.")
        firebase_admin.initialize_app()
        return

    firebase_admin.initialize_app(credentials.Certificate(info))
    logger.info("Firebase initialised for project %s", info["project_id"])


@lru_cache
def get_client() -> Client:
    """The Firestore client. Cached: it holds a connection pool."""
    init_firebase()
    client: Client = firestore.client()
    return client


def user_doc(uid: str) -> Any:
    """The account record for one trader."""
    return get_client().collection(_USERS).document(uid)


def trades_collection(uid: str) -> Any:
    """A trader's journal. Always reached through their own document, so a
    query can never be built that spans two accounts."""
    return user_doc(uid).collection("trades")


def insights_collection(uid: str) -> Any:
    """Model-written analysis. Read by the client, written only from here."""
    return user_doc(uid).collection("insights")
