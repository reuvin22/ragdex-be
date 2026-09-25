#!/usr/bin/env python
"""Create the portfolio demo account, confirmed and on the Free plan.

    python scripts/create_demo_account.py                          # preview
    python scripts/create_demo_account.py --write                  # commit
    python scripts/create_demo_account.py --write --password hunter2
    python scripts/create_demo_account.py --write --email you@example.com

An account someone can sign into from a portfolio link without a real inbox
behind it. Sign-up normally mails a confirmation link and holds the account at
the gate screen until it is clicked; there is nobody to click it here, so this
writes the confirmed state directly instead of pretending to deliver mail.

Two flags mean "verified", and a demo account needs both:

  * ``email_verified`` on the Firebase auth record. Set by clicking Firebase's
    own link, or by arriving through Google.
  * ``confirmedAt`` on the profile document. This is the one the app actually
    gates on -- see ``profiles.is_confirmed`` and the gate in ``App.tsx``.
    Deliberately separate, so a Google account, which is born with
    ``email_verified`` already true, is still held at the door.

Setting only the first leaves an account that signs in and lands on
"confirm your address" forever, which is the failure worth naming.

The plan is Free, which is already ``billing.DEFAULT_PLAN`` -- an account with
no billing record reads as Free on its own. The record is written anyway, so
the demo's tier is a stated fact rather than a default that a later change to
``DEFAULT_PLAN`` would quietly move.

Re-running is safe. An existing account is updated in place, keeping its uid
and its journal, and the password is left alone unless ``--password`` says
otherwise -- so a second run cannot silently invalidate a link already sent to
somebody.

To give the demo something to show, seed a journal afterwards:

    python scripts/seed_journal.py --email <the address> --write
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firebase_admin import auth as firebase_auth  # noqa: E402
from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # noqa: E402

from app.core.security import CurrentUser  # noqa: E402
from app.db.firestore import billing_doc, init_firebase, profile_doc  # noqa: E402
from app.models.repositories import directory  # noqa: E402

DEFAULT_EMAIL = "demo@ragdex.app"
DEFAULT_NAME = "Demo Trader"

#: Free, stated rather than inherited. See the module docstring.
PLAN = "free"

#: A starter profile, so the app opens on the product instead of on a form.
#:
#: Keys are Firestore's, matching ``_FIELDS`` in ``repositories.profiles`` --
#: this writes the document that repository reads, so the two have to agree.
#: The strategies are the ones ``seed_journal.py`` trades, so a seeded journal
#: and this profile describe the same trader.
PROFILE: dict[str, object] = {
    "accountType": "individual",
    "timezone": "America/New_York",
    "currency": "USD",
    "openingBalance": 25_000,
    "tradingStyle": "Day trading",
    "markets": ["Stocks", "Futures"],
    "marketType": "stocks",
    "fundingType": "personal",
    "bio": "Demo account for the RagDex portfolio walkthrough.",
    "accountSize": 25_000,
    "riskPerTradePct": 1,
    "maxDailyLossPct": 3,
    "targetR": 2,
    "maxTradesPerDay": 4,
    "strategies": [
        "VWAP Reclaim",
        "Trend Pullback",
        "Breakout Retest",
        "Opening Range Break",
        "Mean Reversion",
    ],
    "tradingRules": (
        "No trades in the first fifteen minutes. One R of risk per trade. "
        "Stop for the day after two losers."
    ),
    "leakCadence": "daily",
    "edgeWindow": "monthly",
}

#: No ambiguous characters, so the password survives being read off a page.
_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_password() -> str:
    """A password worth sharing, generated rather than committed.

    Deliberately not a constant in this file: a default baked into the
    repository is a credential in git history, and this one is meant to be
    handed out -- which makes rotating it something you should be able to do
    by re-running with ``--password``.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(16))


def _find(email: str) -> firebase_auth.UserRecord | None:
    try:
        return firebase_auth.get_user_by_email(email)
    except firebase_auth.UserNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="The demo address.")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Display name.")
    parser.add_argument(
        "--password",
        help=(
            "Set this password. Omitted, a new account gets a generated one "
            "and an existing account keeps the password it has."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Commit. Without it, only report what would happen.",
    )
    args = parser.parse_args()

    init_firebase()

    existing = _find(args.email)
    password = args.password or (None if existing else _generate_password())

    if password is None:
        how = "unchanged"
    elif args.password:
        how = "as given"
    else:
        how = "generated, printed below"

    print(f"email     {args.email}")
    print(f"name      {args.name}")
    print(f"plan      {PLAN}")
    print(f"account   {'update ' + existing.uid if existing else 'create'}")
    print(f"password  {how}")
    print("verified  email_verified on the auth record, confirmedAt on the profile")

    if not args.write:
        print("\nDry run. Nothing was written. Re-run with --write to commit.")
        return 0

    if existing is None:
        record = firebase_auth.create_user(
            email=args.email,
            password=password,
            display_name=args.name,
            email_verified=True,
        )
        print(f"\ncreated auth record {record.uid}")
    else:
        changes: dict[str, object] = {
            "display_name": args.name,
            "email_verified": True,
            "disabled": False,
        }
        if password is not None:
            changes["password"] = password
        record = firebase_auth.update_user(existing.uid, **changes)
        print(f"\nupdated auth record {record.uid}")

    uid = record.uid

    # The gate. Without confirmedAt the account signs in and lands on the
    # confirm-your-address screen, whatever the auth record says.
    document: dict[str, object] = {
        "email": args.email,
        "displayName": args.name,
        "confirmedAt": SERVER_TIMESTAMP,
        "verifiedAt": SERVER_TIMESTAMP,
        "lastSeenAt": SERVER_TIMESTAMP,
        **PROFILE,
    }
    reference = profile_doc(uid)
    if not reference.get().exists:
        document["createdAt"] = SERVER_TIMESTAMP
    reference.set(document, merge=True)
    print("wrote profile, confirmedAt set")

    billing_doc(uid).set(
        {"uid": uid, "plan": PLAN, "planSince": SERVER_TIMESTAMP}, merge=True
    )
    print(f"wrote billing record, plan {PLAN!r}")

    # What sign-in would publish anyway. Done here so the account is findable
    # in contact search before its first sign-in rather than after.
    directory.publish(
        CurrentUser(
            uid=uid,
            email=args.email,
            email_verified=True,
            name=args.name,
            picture=record.photo_url or "",
        )
    )
    print("published directory entry")

    print("\nDone. Sign in with:")
    print(f"  email     {args.email}")
    print(f"  password  {password if password is not None else '(unchanged)'}")
    print("\nGive it a journal to show:")
    print(f"  python scripts/seed_journal.py --email {args.email} --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
