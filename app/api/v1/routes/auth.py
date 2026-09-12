"""Sign-in, sign-out, and who-am-I.

Everything the browser used to do with the Firebase SDK happens here instead.
The client posts an email and a password and receives a cookie; it never holds
a token, a refresh token, or a Firebase configuration.

Two rate limits apply, and they are unusual for this codebase in being keyed by
IP rather than uid — these are the only routes reachable without a session, so
there is no uid to key on, and they are exactly the routes worth limiting.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import RedirectResponse
from firebase_admin import auth as firebase_auth

from app.api.deps import AppSettings, MaybeUser, ReadUser
from app.core import confirmation
from app.core import session as session_store
from app.core.errors import AppError
from app.core.security import AuthError, CurrentUser, anonymous_rate_limit
from app.repositories import profiles as profile_repo
from app.schemas.auth import (
    Credentials,
    GoogleSignIn,
    PasswordResetRequest,
    Registration,
    Session,
    SessionUser,
    VerificationSent,
)
from app.schemas.common import ErrorResponse, Message
from app.services import email as email_service
from app.services import identity

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)


def _user_from(record: firebase_auth.UserRecord, *, confirmed: bool) -> SessionUser:
    return SessionUser(
        uid=record.uid,
        email=record.email,
        email_verified=bool(record.email_verified),
        display_name=record.display_name or "",
        photo_url=record.photo_url or "",
        providers=[entry.provider_id for entry in record.provider_data],
        confirmed=confirmed,
    )


def _start_session(
    response: Response, result: identity.IdentityResult, settings: AppSettings
) -> SessionUser:
    """Turn a just-proven identity into a cookie, and answer with who it is.

    The ID token is used once, here, and dropped. It is the only moment one
    exists outside Identity Toolkit, and it never leaves this process.
    """
    cookie = session_store.mint(result.id_token, settings)
    session_store.attach(response, cookie, settings)

    record = firebase_auth.get_user(result.uid)
    # The account record is upserted on sign-in, so a first-time caller has one
    # before any other route needs it.
    profile_repo.record_sign_in(
        CurrentUser(
            uid=record.uid,
            email=record.email,
            email_verified=bool(record.email_verified),
            name=record.display_name,
        )
    )
    return _user_from(record, confirmed=profile_repo.is_confirmed(record.uid))


@router.get("/session", response_model=Session, summary="Who am I")
async def read_session(user: MaybeUser) -> Session:
    """Answers 200 with a null user when nobody is signed in.

    The client calls this once on load to choose between the app, the login
    screen and the verify-email gate, so "not signed in" is an ordinary answer
    here rather than an error.
    """
    if user is None:
        return Session(user=None)

    # Read through to the auth record rather than trusting the cookie's claims:
    # a cookie minted before someone confirmed their address still says
    # unverified, and the gate screen polls this to notice the change.
    record = firebase_auth.get_user(user.uid)
    return Session(user=_user_from(record, confirmed=profile_repo.is_confirmed(user.uid)))


@router.post(
    "/register",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    dependencies=[anonymous_rate_limit("register", 10)],
)
async def register(
    payload: Registration, response: Response, settings: AppSettings
) -> Session:
    """Create the account and sign in.

    No verification email is sent from here. The gate screen asks for it, so
    the branded Brevo message is the only one that goes out — firing Firebase's
    default template as well would deliver an unbranded duplicate.
    """
    result = await identity.sign_up(payload.email, payload.password, settings)

    name = payload.display_name.strip()
    if name:
        await identity.set_display_name(result.id_token, name, settings)

    return Session(user=_start_session(response, result, settings))


@router.post(
    "/login",
    response_model=Session,
    summary="Sign in",
    dependencies=[anonymous_rate_limit("login", 20)],
)
async def login(
    payload: Credentials, response: Response, settings: AppSettings
) -> Session:
    result = await identity.sign_in(payload.email, payload.password, settings)
    return Session(user=_start_session(response, result, settings))


@router.post("/logout", response_model=Message, summary="Sign out")
async def logout(user: MaybeUser, response: Response, settings: AppSettings) -> Message:
    """Clears the cookie, and revokes every session when we know whose it is.

    Deliberately does not require a valid session. Demanding one made signing
    out impossible in exactly the situations where it matters most: an expired
    cookie, a revoked one, an account disabled mid-session. The client cannot
    clear an HttpOnly cookie itself, so a 401 here left the browser holding a
    dead cookie with no way to drop it.

    So this always succeeds. Clearing is unconditional; revoking needs a uid
    and is skipped when there is none — there is nothing to revoke for a caller
    we cannot identify, and nothing is leaked by saying so.
    """
    if user is not None:
        # The stronger behaviour and the one worth having: a session someone
        # wants ended is usually one they have lost control of, so end them all.
        session_store.revoke(user.uid)

    session_store.clear(response, settings)
    return Message(message="Signed out.")


@router.post(
    "/verify-email",
    response_model=VerificationSent,
    summary="Send the confirmation email",
)
async def send_verification(user: ReadUser, settings: AppSettings) -> VerificationSent:
    """Send the branded confirm-your-address email.

    The address comes from the account this session names, never from a body —
    otherwise this would be a way to aim mail at someone else's inbox from our
    domain.

    The link is ours, not Firebase's. A Google account arrives with
    ``email_verified`` already true, so Firebase has nothing left to verify and
    its own link would be a no-op; confirmation has to be something this service
    issues and this service records.
    """
    record = firebase_auth.get_user(user.uid)

    if profile_repo.is_confirmed(user.uid):
        return VerificationSent(status="already-verified")
    if not record.email:
        raise AppError("This account has no email address.", code="no_email")

    if not email_service.is_configured(settings):
        raise AppError(
            "Email sending is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )

    # Refuse before doing the work, so a caller on cooldown does not burn a
    # send to be told no.
    email_service.check_cooldown(user.uid)

    # Through the app's own origin, which the rewrite proxies here. A link in
    # an email outlives the page that asked for it, so it points at the address
    # people actually have, not this service's hostname.
    token = confirmation.issue(user.uid, settings)
    base = settings.app_url.rstrip("/")
    link = f"{base}{settings.api_v1_prefix}/auth/confirm?token={token}"

    await email_service.send_verification(
        uid=user.uid,
        email=record.email,
        name=record.display_name or "",
        link=link,
        settings=settings,
    )
    return VerificationSent(status="sent")


@router.get("/confirm", summary="Confirm an email address", include_in_schema=False)
async def confirm(
    settings: AppSettings, token: str = Query(default="")
) -> RedirectResponse:
    """Where the confirmation link lands.

    Reached by clicking a link in an email, so it answers with a redirect in
    every case — there is nobody here to read JSON. The outcome rides back on a
    query parameter for the login screen to explain.

    Deliberately needs no session: the link often opens in a different browser
    from the one that asked for it, and requiring a cookie would make those
    clicks fail for no gain. The signature is what authorises this, and it names
    the uid itself, so there is nothing for a caller to choose.
    """
    app_url = settings.app_url.rstrip("/")

    try:
        uid = confirmation.redeem(token, settings)
    except AppError as exc:
        logger.warning("Confirmation rejected: %s", exc.code)
        return RedirectResponse(
            f"{app_url}/?confirm=invalid#/login", status_code=status.HTTP_303_SEE_OTHER
        )

    profile_repo.mark_confirmed(uid)

    # Mirror it onto the auth record too, so the flag that gates writes agrees
    # with the one that gates the door.
    try:
        firebase_auth.update_user(uid, email_verified=True)
    except Exception:
        logger.warning("Could not mark %s verified on the auth record", uid)

    return RedirectResponse(
        f"{app_url}/?confirm=ok#/login", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post(
    "/password-reset",
    response_model=Message,
    summary="Send a password reset email",
    dependencies=[anonymous_rate_limit("password_reset", 5)],
)
async def password_reset(
    payload: PasswordResetRequest, settings: AppSettings
) -> Message:
    """Always answers the same way.

    Whether an address has an account is not something an unauthenticated
    caller should be able to test, so a miss and a hit are indistinguishable
    from out here.
    """
    try:
        await identity.send_password_reset(payload.email, settings)
    except AppError as exc:
        # A genuine outage is worth knowing about; "no such user" is not.
        if exc.code not in {"invalid_credentials", "invalid_email"}:
            logger.warning("Password reset failed: %s", exc.code)

    return Message(message="If that address has an account, a reset email is on its way.")


# ----------------------------------------------------------------- Google

@router.post(
    "/google",
    response_model=Session,
    summary="Sign in with a Google account",
    dependencies=[anonymous_rate_limit("google", 20)],
)
async def google_sign_in(
    payload: GoogleSignIn, response: Response, settings: AppSettings
) -> Session:
    """Exchange a Firebase ID token for a session cookie.

    The browser runs the Google popup through the Firebase SDK and arrives here
    with an ID token. This service verifies it with the Admin SDK — so the
    token's signature, audience and expiry are all checked against the Firebase
    project, and a token minted for some other project is refused — then mints
    the same HttpOnly cookie every other sign-in produces.

    Deliberately not the OAuth redirect dance this replaced. Firebase's SDK
    uses the OAuth client it provisions itself, which removes the entire class
    of redirect-URI and cross-project mismatches: there is no client id here,
    no secret, and no callback to register.

    What the browser holds is still only a cookie it cannot read. The ID token
    lives for the duration of this request and is then dropped.
    """
    token = payload.id_token

    try:
        # check_revoked so a signed-out or disabled account cannot present an
        # ID token it kept. Costs a lookup; worth it on the one route that
        # turns a token into a session.
        claims = firebase_auth.verify_id_token(token, check_revoked=True)
    except firebase_auth.UserDisabledError as exc:
        raise AppError(
            "This account is disabled.",
            status_code=status.HTTP_403_FORBIDDEN,
            code="account_disabled",
        ) from exc
    except Exception as exc:
        # The reason goes to the log, not the caller: telling someone probing
        # whether a token was expired, malformed or wrongly signed helps only
        # them.
        logger.warning("Google ID token rejected: %s", type(exc).__name__)
        raise AuthError("Could not verify that Google sign-in.") from exc

    result = identity.IdentityResult(
        uid=str(claims["uid"]),
        email=str(claims.get("email", "")),
        id_token=token,
        display_name=str(claims.get("name", "")),
        photo_url=str(claims.get("picture", "")),
    )
    return Session(user=_start_session(response, result, settings))
