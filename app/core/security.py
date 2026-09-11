"""Authentication and the request-level protections around it.

The only identity this API trusts is a Firebase ID token it has verified
itself. A uid is never read from a path, a query string or a body — if a
client could name the account it is acting on, every ownership check in the
repositories would be decorative.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth

from app.core.config import Settings, get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

# auto_error=False so a missing header produces our error envelope rather than
# Starlette's bare {"detail": ...}.
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Firebase ID token")


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The verified caller. Constructed only from a checked token."""

    uid: str
    email: str | None
    email_verified: bool
    name: str | None


class AuthError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message, status_code=status.HTTP_401_UNAUTHORIZED, code="unauthorized"
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    """Verify the bearer token and return who sent it.

    ``check_revoked`` costs a lookup but means a signed-out or disabled
    session stops working immediately rather than at token expiry.
    """
    if credentials is None or not credentials.credentials:
        raise AuthError("Sign in to continue.")

    try:
        claims = firebase_auth.verify_id_token(
            credentials.credentials, check_revoked=True
        )
    except firebase_auth.ExpiredIdTokenError as exc:
        raise AuthError("Your session expired. Sign in again.") from exc
    except firebase_auth.RevokedIdTokenError as exc:
        raise AuthError("Your session was ended. Sign in again.") from exc
    except firebase_auth.UserDisabledError as exc:
        raise AuthError("This account is disabled.") from exc
    except Exception as exc:  # noqa: BLE001 - any failure is a failed token
        # The reason is for us, not for the caller: distinguishing "malformed"
        # from "wrong signature" only helps someone probing.
        logger.warning("Token verification failed: %s", type(exc).__name__)
        raise AuthError("Could not verify your session.") from exc

    return CurrentUser(
        uid=claims["uid"],
        email=claims.get("email"),
        email_verified=bool(claims.get("email_verified", False)),
        name=claims.get("name"),
    )


async def get_verified_user(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """A caller who has confirmed their email address.

    Mirrors the Firestore rules, which refuse writes from unverified sessions.
    Anything that writes should depend on this rather than on
    ``get_current_user``.
    """
    if not user.email_verified:
        raise AppError(
            "Confirm your email address first.",
            status_code=status.HTTP_403_FORBIDDEN,
            code="email_unverified",
        )
    return user


class RateLimiter:
    """A fixed-window limiter, keyed per caller.

    In-process on purpose: it is a guard rail for a single instance, not a
    distributed quota. Running more than one replica means moving this to
    Redis — the interface is the same, so only the storage changes.
    """

    def __init__(self, per_minute: int, *, name: str) -> None:
        self.per_minute = per_minute
        self.name = name
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self._hits[key]

        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= self.per_minute:
            raise AppError(
                "Too many requests. Wait a moment and try again.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
            )

        window.append(now)


_limiters: dict[str, RateLimiter] = {}


def rate_limit(name: str, per_minute_attr: str = "rate_limit_per_minute"):
    """Build a dependency that limits one route family per user."""

    async def _dependency(
        request: Request,
        user: CurrentUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ) -> None:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(getattr(settings, per_minute_attr), name=name)
            _limiters[name] = limiter

        # Keyed by uid, so one noisy client cannot spend another's budget, and
        # a shared IP (an office, a mobile carrier) is not punished.
        limiter.check(f"{name}:{user.uid}")
        request.state.rate_limited_as = user.uid

    return _dependency
