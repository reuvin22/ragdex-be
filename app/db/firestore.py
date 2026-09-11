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
import google.auth
from firebase_admin import credentials, firestore
from google.auth.exceptions import DefaultCredentialsError
from google.cloud.firestore_v1 import Client

from app.core.config import SERVICE_ACCOUNT_PATHS, get_settings

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
        #
        # Anywhere else this is a misconfiguration, and it used to be a silent
        # one: the app started, /health said "ok", and every call that actually
        # touched Firebase failed later with DefaultCredentialsError. Sign-in
        # got all the way through Google and then died minting the session
        # cookie, which made it look like an OAuth problem for hours.
        #
        # So ask for the credentials rather than assume they resolve.
        # initialize_app() succeeds with none and defers the failure to the
        # first call, which is exactly how this stayed hidden.
        try:
            google.auth.default()
        except DefaultCredentialsError as exc:
            raise RuntimeError(
                "No Firebase credentials. Provide them one of three ways:\n"
                "  - a service account file at one of "
                f"{', '.join(str(p) for p in SERVICE_ACCOUNT_PATHS)} "
                "(on Render: upload it under Secret Files)\n"
                "  - FIREBASE_SERVICE_ACCOUNT_FILE, a path to that JSON\n"
                "  - FIREBASE_SERVICE_ACCOUNT, the same JSON on one line\n"
                "  - Application Default Credentials, on Cloud Run or GKE"
            ) from exc

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
