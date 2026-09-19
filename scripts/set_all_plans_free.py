#!/usr/bin/env python
"""Put every existing account on the Free plan.

    python scripts/set_all_plans_free.py            # preview
    python scripts/set_all_plans_free.py --write    # commit

Deploy the schema change first. Before it, the API does not know "free" and
reads it back as "individual" — nothing breaks, but nothing changes either.

Only billing records are touched, because a billing record is the only place a
plan other than the default can live. An account with no record already reads
as the default, which is now Free, so it needs no write. Profiles and journals
are never read or written.

Re-running is safe: accounts already on Free are skipped, so a second pass
writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # noqa: E402

from app.db.firestore import billings_collection, get_client, init_firebase  # noqa: E402

TARGET = "free"

#: Firestore commits at most 500 writes per batch.
BATCH_LIMIT = 500


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Commit the change. Without it, only report what would change.",
    )
    args = parser.parse_args()

    init_firebase()

    records = list(billings_collection().stream())
    before = Counter((record.to_dict() or {}).get("plan", "(none)") for record in records)
    pending = [record for record in records if (record.to_dict() or {}).get("plan") != TARGET]

    print(f"billing records  {len(records)}")
    for plan, count in sorted(before.items()):
        print(f"  {plan:<12} {count}")
    print(f"\nto move to {TARGET!r}  {len(pending)}")
    print("accounts with no billing record already read as free and are not written.")

    if not args.write:
        print("\nDry run. Nothing was written. Re-run with --write to commit.")
        return 0

    client = get_client()
    written = 0
    for start in range(0, len(pending), BATCH_LIMIT):
        batch = client.batch()
        for record in pending[start : start + BATCH_LIMIT]:
            batch.set(
                record.reference,
                {"plan": TARGET, "planSince": SERVER_TIMESTAMP},
                merge=True,
            )
        batch.commit()
        written += len(pending[start : start + BATCH_LIMIT])
        print(f"  committed {written}/{len(pending)}")

    print(f"\nDone. {written} account(s) moved to {TARGET!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
