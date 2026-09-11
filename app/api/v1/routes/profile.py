"""The signed-in trader's own account record.

Singular, and with no uid in the path: "me" is the only account any caller
can address.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ReadUser, StandardRateLimit, WriteUser
from app.repositories import profiles as repo
from app.schemas.common import ErrorResponse
from app.schemas.profile import PlanChange, Profile, ProfileUpdate

router = APIRouter(
    prefix="/me",
    tags=["profile"],
    dependencies=[StandardRateLimit],
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)


@router.get("", response_model=Profile, summary="Read your account")
async def read_profile(user: ReadUser) -> Profile:
    """Upserts on read, so a first sign-in has a record without a separate
    call — the same contract the web client already relies on."""
    return repo.record_sign_in(user)


@router.patch("", response_model=Profile, summary="Edit your account")
async def update_profile(user: WriteUser, payload: ProfileUpdate) -> Profile:
    return repo.update_profile(user.uid, payload)


@router.put("/plan", response_model=Profile, summary="Change your plan")
async def change_plan(user: WriteUser, payload: PlanChange) -> Profile:
    """Records the chosen plan so the app can gate features.

    No money moves here: there is no payment processor connected, and this
    endpoint deliberately does not pretend otherwise.
    """
    return repo.set_plan(user.uid, payload.plan)
