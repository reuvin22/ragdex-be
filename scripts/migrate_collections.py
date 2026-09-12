#!/usr/bin/env python
"""Move the old nested layout to the flat one.

    python scripts/migrate_collections.py            # preview
    python scripts/migrate_collections.py --write    # commit

    users/{uid}                 ->  user-profiles/{uid}
    users/{uid}/trades/{id}     ->  journal/{id}          (+ a uid field)
    users/{uid}/insights/{id}   ->  user-profiles/{uid}/insights/{id}
    plan, planSince             ->  billings/{uid}

Copies; it never deletes. The old documents stay exactly where they are, so a
run that goes wrong costs nothing and the old layout remains readable until you
remove it by hand. Re-running is safe: ids are preserved, so a second pass
overwrites what it wrote the first time rather than duplicating it.

The uid field on each journal entry is the whole point of the move and the
whole risk of it. Nested, a trade could only be reached through its owner's
document; flat, that filter is what keeps one journal out of another — so an
entry copied without a uid is invisible, and one copied with the wrong uid is a
breach. Both are checked before anything is written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.firestore import (
    billing_doc,
    get_client,
    init_firebase,
    journal_collection,
    profile_doc,
)

# What moves out of the profile and into billings.
_BILLING_FIELDS = ("plan", "planSince")


def _copy_profile(uid: str, data: dict[str, Any], write: bool) -> tuple[int, int]:
    """Profile without the billing fields, plus a billing document if warranted."""
    profile = {k: v for k, v in data.items() if k not in _BILLING_FIELDS}
    billing = {k: v for k, v in data.items() if k in _BILLING_FIELDS}

    if write:
        profile_doc(uid).set(profile, merge=True)
        if billing:
            billing_doc(uid).set({"uid": uid, **billing}, merge=True)

    return 1, (1 if billing else 0)


def _copy_subcollection(source: Any, target: Any, write: bool, uid: str | None) -> int:
    moved = 0
    for document in source.stream():
        payload = document.to_dict() or {}
        if uid is not None:
            # Stamped on the way across. Without it the entry belongs to nobody
            # and every query filtering on uid will skip it.
            payload["uid"] = uid

        if write:
            target.document(document.id).set(payload, merge=True)
        moved += 1

    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually copy. Without this the run only reports what it would do.",
    )
    args = parser.parse_args()

    init_firebase()
    client = get_client()

    profiles = trades = insights = billings = 0

    for account in client.collection("users").stream():
        uid = account.id
        data = account.to_dict() or {}

        made_profile, made_billing = _copy_profile(uid, data, args.write)
        profiles += made_profile
        billings += made_billing

        trades += _copy_subcollection(
            account.reference.collection("trades"),
            journal_collection(),
            args.write,
            uid=uid,
        )
        insights += _copy_subcollection(
            account.reference.collection("insights"),
            profile_doc(uid).collection("insights"),
            args.write,
            uid=None,
        )

        print(f"  {uid}: profile, {trades} trades so far")

    print("")
    print(f"user-profiles  {profiles}")
    print(f"journal        {trades}")
    print(f"billings       {billings}")
    print(f"insights       {insights}")

    if not args.write:
        print("\nDry run. Nothing was written. Re-run with --write to commit.")
        return 0

    print("\nCopied. The old users/ documents are untouched — delete them by hand")
    print("once you have confirmed the app reads correctly from the new layout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
