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

# Top-level collections. Flat rather than nested under an account document,
# which is a deliberate trade: it makes every collection independently
# queryable and exportable, and it moves tenant isolation from the shape of a
# path to a filter that has to be applied. See _owned() in repositories/trades.
_PROFILES = "user-profiles"
_JOURNAL = "journal"
_BILLINGS = "billings"
_ENROLMENTS = "enrolments"
_UNIVERSITIES = "universities"
_UNIVERSITY_DOCS = "university-docs"
_ARENA = "arena"
_TOURNAMENTS = "tournaments"
_BATTLE_QUEUE = "battle-queue"
_BATTLES = "battles"
_POSTS = "posts"
_BROKERS = "broker-connections"


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


def profile_doc(uid: str) -> Any:
    """The account record for one trader, in user-profiles."""
    return get_client().collection(_PROFILES).document(uid)


def journal_collection() -> Any:
    """Every trade, from every account.

    Flat, so it carries a uid field and every query must filter on it. Nothing
    should call this directly except the journal repository, which owns the one
    helper that applies that filter — reach for repositories.trades instead.
    """
    return get_client().collection(_JOURNAL)


def billing_doc(uid: str) -> Any:
    """One trader's plan. A document per account, keyed by uid."""
    return get_client().collection(_BILLINGS).document(uid)


def billings_collection() -> Any:
    """Every billing record. For maintenance scripts; routes address one uid."""
    return get_client().collection(_BILLINGS)


def broker_doc(uid: str) -> Any:
    """One trader's broker connection. A document per account, keyed by uid.

    Its own collection rather than a field on the profile: it holds a live
    credential, it is read on a different schedule from everything else, and
    keeping it apart means a rule or an export that covers profiles does not
    silently cover access tokens too.
    """
    return get_client().collection(_BROKERS).document(uid)


def insights_collection(uid: str) -> Any:
    """Model-written analysis. Read by the client, written only from here.

    Still a subcollection of the profile: it is meaningless without the account
    it describes, and nothing ever queries across accounts for it.
    """
    return profile_doc(uid).collection("insights")


def conversation_doc(uid: str) -> Any:
    """One trader's coach conversation.

    A single document rather than a subcollection of messages: it is only ever
    read and written whole, it is capped at a few dozen turns, and one document
    read on page load beats a query every time.
    """
    return profile_doc(uid).collection("coach").document("conversation")


def enrolments_collection() -> Any:
    """Who coaches whom.

    Flat, keyed ``<coachUid>_<studentUid>``. Deterministic on purpose: it makes
    a second invite to the same person an overwrite rather than a duplicate,
    and it lets accept and decline address a row without the client holding an
    id it was handed earlier.
    """
    return get_client().collection(_ENROLMENTS)


def posts_collection() -> Any:
    """The community feed.

    The one collection in this service that is read across accounts by design.
    Everything in a post is already published — a display name and a photo the
    directory carries anyway — and nothing is copied here from the journal.
    """
    return get_client().collection(_POSTS)


def university_doc(coach_uid: str) -> Any:
    """One coach's program: its name, and the invitation email they wrote.

    Keyed by the coach rather than by a generated id, because a coach has one
    program and looking it up should not need a query.
    """
    return get_client().collection(_UNIVERSITIES).document(coach_uid)


def documents_collection() -> Any:
    """Agreements and forms a coach puts in front of their students.

    Flat and carrying a ``coachUid``, like the journal carries a uid: every
    query filters on it, and the one place that does not — reading a single
    document by id — is followed immediately by an ownership or enrolment
    check in the route.
    """
    return get_client().collection(_UNIVERSITY_DOCS)


def arena_collection() -> Any:
    """Competitors on the ladder, one document per entrant.

    Only entrants are here — leaving deletes the row. That is what lets the
    leaderboard be a single ordering with no filter beside it.
    """
    return get_client().collection(_ARENA)


def tournaments_collection() -> Any:
    """Tournaments, and their entrants as a subcollection."""
    return get_client().collection(_TOURNAMENTS)


def battle_queue() -> Any:
    """Traders waiting for an opponent. One document each, deleted on pairing."""
    return get_client().collection(_BATTLE_QUEUE)


def battles_collection() -> Any:
    """Matches, settled and unsettled."""
    return get_client().collection(_BATTLES)
