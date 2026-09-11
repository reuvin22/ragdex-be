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
from app.core import session as session_store
from app.core.errors import AppError
from app.core.security import CurrentUser, anonymous_rate_limit
from app.repositories import profiles as profile_repo
from app.schemas.auth import (
    Credentials,
    PasswordResetRequest,
    Registration,
    Session,
    SessionUser,
    VerificationSent,
)
from app.schemas.common import ErrorResponse, Message
from app.services import email as email_service
from app.services import google_oauth, identity

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)


def _user_from(record: firebase_auth.UserRecord) -> SessionUser:
    return SessionUser(
        uid=record.uid,
        email=record.email,
        email_verified=bool(record.email_verified),
        display_name=record.display_name or "",
        photo_url=record.photo_url or "",
        providers=[entry.provider_id for entry in record.provider_data],
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
    return _user_from(record)


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
    return Session(user=_user_from(record))


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
async def logout(user: ReadUser, response: Response, settings: AppSettings) -> Message:
    """Clears the cookie and revokes every session this account has.

    Revoking is the stronger behaviour and the one worth having: a session
    someone wants ended is usually one they have lost control of.
    """
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
    """
    record = firebase_auth.get_user(user.uid)

    if record.email_verified:
        return VerificationSent(status="already-verified")
    if not record.email:
        raise AppError("This account has no email address.", code="no_email")

    if not email_service.is_configured(settings):
        raise AppError(
            "Email sending is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )

    # Refuse before doing the work, so a caller on cooldown does not burn an
    # Identity Toolkit call to be told no.
    email_service.check_cooldown(user.uid)

    # A fresh ID token is needed to ask for the link, and minting one for our
    # own use never puts it anywhere a browser can reach.
    custom = firebase_auth.create_custom_token(user.uid)
    signed_in = await identity.exchange_custom_token(custom.decode(), settings)
    link = await identity.verification_link(signed_in, settings)

    await email_service.send_verification(
        uid=user.uid,
        email=record.email,
        name=record.display_name or "",
        link=link,
        settings=settings,
    )
    return VerificationSent(status="sent")


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

@router.get(
    "/google/start",
    summary="Begin Google sign-in",
    dependencies=[anonymous_rate_limit("google", 20)],
)
async def google_start(settings: AppSettings) -> RedirectResponse:
    """Send the browser to Google.

    The page that calls this holds no client id and no secret — it navigates
    here, and this service builds the authorize URL.
    """
    state = google_oauth.make_state(settings)
    return RedirectResponse(
        google_oauth.authorize_url(state, settings),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/google/callback", summary="Finish Google sign-in", include_in_schema=False)
async def google_callback(
    settings: AppSettings,
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    """Where Google returns the browser.

    Answers with a redirect in every case, success or failure: this URL is
    reached by a navigation, not by fetch, so the only way to report anything
    is to put it in the query string of the page we send them back to.
    """
    app_url = settings.app_url.rstrip("/")

    def back(reason: str | None = None) -> RedirectResponse:
        target = f"{app_url}/#/login"
        if reason:
            target = f"{app_url}/?auth_error={reason}#/login"
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    if error or not code:
        # The user pressed cancel, or Google refused. Neither is our failure.
        return back("cancelled")

    # Named so a failure says which of the three steps it died in. Without
    # this the log reads "google sign-in failed" for a bad state, a refused
    # code exchange and an unconfigured Identity Toolkit alike, which are
    # three very different problems to go and fix.
    stage = "state"
    try:
        google_oauth.check_state(state, settings)

        stage = "code_exchange"
        id_token = await google_oauth.exchange_code(code, settings)

        stage = "firebase_sign_in"
        # The configured callback, not request.base_url. Starlette derives
        # base_url from forwarded headers, and behind a proxy that does not
        # reach it — on Render it comes out http:// — while Identity Toolkit
        # validates this field. Deriving it from settings makes it the same
        # string we sent Google, which is what it is supposed to be.
        result = await identity.sign_in_with_google(
            id_token, google_oauth.redirect_uri(settings), settings
        )
    except AppError as exc:
        logger.warning("Google sign-in failed at %s: %s", stage, exc.code)
        # "Not configured" is a deployment mistake, not a secret — saying so
        # turns an unactionable "try again" into something the person running
        # this can actually go and fix. Everything else stays generic.
        return back("unconfigured" if exc.code == "not_configured" else "failed")

    response = back()
    _start_session(response, result, settings)
    return response
