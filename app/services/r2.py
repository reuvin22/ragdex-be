"""Signed URLs for Cloudflare R2.

R2 speaks S3, so these are ordinary AWS Signature Version 4 query-string
presigned URLs. They are generated here rather than by boto3: signing is a
hundred lines of HMAC that this service can do with the standard library, and
botocore is the better part of a hundred megabytes to add to a container whose
only other job is to talk to Firebase.

The point of presigning is that the browser never holds a credential. It is
handed a URL that permits exactly one operation, on exactly one object, for a
few minutes, and nothing else — the secret stays here.

What the signature binds is the interesting part. `host`, `content-type` and
`content-length` are all signed, so a URL issued for a 2MB JPEG cannot be used
to upload a 900MB one, or an HTML file: change any of them and the signature
stops matching. Without content-length in there the size cap would be a request
the client could simply ignore.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from app.core.config import Settings
from app.core.errors import AppError

#: R2 has no regions, but SigV4 demands one and R2 expects this exact value.
_REGION = "auto"
_SERVICE = "s3"
_ALGORITHM = "AWS4-HMAC-SHA256"

#: The formats a browser can produce and this app is willing to store. Kept
#: narrow on purpose: an "image" the server will happily sign a URL for is an
#: upload slot, and SVG in particular is a script-delivery format.
ALLOWED_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    #: Chat images arrive already encrypted, so their bytes are not an image at
    #: all and no sniffing will say otherwise.
    "application/octet-stream": "bin",
}


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, stamp: str) -> bytes:
    """The chained key SigV4 derives per day, per region, per service."""
    date = _sign(f"AWS4{secret}".encode(), stamp)
    region = _sign(date, _REGION)
    service = _sign(region, _SERVICE)
    return _sign(service, "aws4_request")


def _host(settings: Settings) -> str:
    return f"{settings.r2_account_id}.r2.cloudflarestorage.com"


#: The only folders the bucket has. A key naming anything else is not a key
#: this service issued, whatever else is true of it.
#:
#: ``university`` is the odd one and worth the sentence. The other four hold
#: things only their owner is shown; this one holds images a coach puts in an
#: invitation email, and an email is read days later by a mail client that
#: will not be carrying anybody's session. So these are fetched through a
#: public redirect (see ``PUBLIC_FOLDERS``) rather than a signed read, and a
#: coach putting something private in a mail template is putting it somewhere
#: public. The editor says so.
FOLDERS = frozenset({"profile", "charts", "ai", "messages", "university"})

#: Folders whose objects are served to anyone who has the key.
#:
#: Deliberately a separate, smaller set. ``FOLDERS`` answers "is this a key we
#: issued"; this answers "may a stranger fetch it", and the two must never
#: drift into being the same question.
PUBLIC_FOLDERS = frozenset({"university"})


def is_public(key: str) -> bool:
    """Whether this object may be served without a session.

    Same positional discipline as :func:`owns`: traversal is refused outright
    and the folder has to be exactly the first segment, so no amount of
    creative naming moves a private object into a public prefix.
    """
    if ".." in key:
        return False

    parts = key.split("/")
    return len(parts) >= 3 and parts[0] in PUBLIC_FOLDERS


def object_key(uid: str, kind: str, content_type: str) -> str:
    """Where one upload lives: `<folder>/<uid>/<random>.<ext>`.

    The uid is the second segment rather than the first, so each category is a
    top-level prefix that lifecycle rules and the dashboard can address. What
    matters for access is that the uid is at a *fixed* position — it is still
    readable off the key with no lookup, which is what lets the read endpoint
    decide ownership from the name alone.
    """
    extension = ALLOWED_TYPES.get(content_type, "bin")
    return f"{kind}/{uid}/{uuid.uuid4().hex}.{extension}"


def owns(uid: str, key: str) -> bool:
    """Whether this key belongs to this trader.

    Positional, not a prefix match: the folder has to be one this service uses
    and the uid has to be exactly the second segment. Matching a prefix would
    let `messages/u1extra/...` pass for `u1`, and checking `uid in key` would
    let it appear anywhere at all.

    Traversal is rejected outright rather than normalised. A key with `..` in
    it is not a near miss to be repaired, it is someone reaching.
    """
    if ".." in key:
        return False

    parts = key.split("/")
    return len(parts) >= 3 and parts[0] in FOLDERS and parts[1] == uid


def readable(uid: str, key: str) -> bool:
    """Whether this trader may be shown this image.

    Wider than :func:`owns` in exactly one place: a profile photo is a
    face people are meant to see. The directory already hands a contact
    someone's name, email and photo, so signing a read for that key
    discloses nothing the caller could not already ask for — and without
    it a contact's avatar in chat is permanently a broken image.

    Everything else stays the owner's: charts, coach images and the
    pictures sent inside a conversation.
    """
    if ".." in key:
        return False

    parts = key.split("/")
    if len(parts) < 3:
        return False

    return parts[0] == "profile" or owns(uid, key)


def _presign(
    *,
    method: str,
    key: str,
    settings: Settings,
    signed_headers: dict[str, str],
) -> str:
    if not settings.r2_configured or settings.r2_secret_access_key is None:
        raise AppError(
            "Image storage is not configured on the server.",
            status_code=501,
            code="not_configured",
        )

    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")

    headers = {"host": _host(settings), **signed_headers}
    # Lower-cased names, sorted, and values trimmed: SigV4 canonicalises both
    # sides, and a mismatch here fails with a signature error that says nothing
    # about which header disagreed.
    names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in names)
    signed = ";".join(names)

    credential = (
        f"{settings.r2_access_key_id}/{stamp}/{_REGION}/{_SERVICE}/aws4_request"
    )

    query = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(settings.r2_url_ttl_seconds),
        "X-Amz-SignedHeaders": signed,
    }
    canonical_query = "&".join(
        f"{quote(name, safe='-_.~')}={quote(query[name], safe='-_.~')}"
        for name in sorted(query)
    )

    # Path style: R2 serves <account>.r2.cloudflarestorage.com/<bucket>/<key>.
    # The key keeps its slashes — they are path separators, not data.
    path = f"/{settings.r2_bucket}/{quote(key, safe='/')}"

    canonical_request = "\n".join(
        [
            method,
            path,
            canonical_query,
            canonical_headers,
            signed,
            # The body is not hashed: it does not exist yet at signing time,
            # and for a browser upload it never passes through this service.
            "UNSIGNED-PAYLOAD",
        ]
    )

    to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            f"{stamp}/{_REGION}/{_SERVICE}/aws4_request",
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(settings.r2_secret_access_key.get_secret_value(), stamp),
        to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return f"https://{_host(settings)}{path}?{canonical_query}&X-Amz-Signature={signature}"


def presign_put(
    *, key: str, content_type: str, content_length: int, settings: Settings
) -> str:
    """A URL that accepts exactly this object, of this type, at this size."""
    return _presign(
        method="PUT",
        key=key,
        settings=settings,
        signed_headers={
            "content-type": content_type,
            "content-length": str(content_length),
        },
    )


def presign_get(*, key: str, settings: Settings) -> str:
    """A URL that reads one object, for as long as the TTL allows."""
    return _presign(method="GET", key=key, settings=settings, signed_headers={})
