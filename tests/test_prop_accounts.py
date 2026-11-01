"""Prop firm accounts on the profile: what is accepted, and how it is stored."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.models.repositories.profiles import _prop_accounts_of, _storable
from app.models.schemas.profile import ProfileUpdate, PropAccount
from pydantic import ValidationError


def _row(**overrides) -> dict:
    defaults = dict(
        id="a1", kind="eval", firm="Topstep", size="50000", price="49", status="active"
    )
    return {**defaults, **overrides}


def test_a_funded_account_carries_no_price() -> None:
    """The firm paid for it. A price left over from the form would count as
    money spent on the dashboard."""
    assert PropAccount.model_validate(_row(kind="funded", price="150")).price is None
    assert PropAccount.model_validate(_row()).price == Decimal("49")


@pytest.mark.parametrize(
    "bad",
    [
        _row(kind="challenge"),
        _row(status="passed"),
        _row(firm=""),
        _row(price="-1"),
        _row(id="../x"),
        {**_row(), "uid": "someone-else"},
    ],
)
def test_bad_rows_are_refused(bad: dict) -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate(prop_accounts=[bad])


def test_market_is_futures_or_cfd_and_optional_on_old_rows() -> None:
    assert PropAccount.model_validate(_row(market="cfd")).market == "cfd"
    assert PropAccount.model_validate(_row()).market is None
    with pytest.raises(ValidationError):
        PropAccount.model_validate(_row(market="crypto"))


def test_prop_firms_are_trimmed_and_listed_once() -> None:
    update = ProfileUpdate(prop_firms=[" Topstep ", "topstep", "", "FTMO"])
    assert update.prop_firms == ["Topstep", "FTMO"]


def test_rows_are_stored_without_decimals() -> None:
    dumped = ProfileUpdate(prop_accounts=[_row()]).model_dump(exclude_unset=True)
    stored = _storable(dumped["prop_accounts"])
    assert stored[0]["size"] == 50000.0
    assert isinstance(stored[0]["price"], float)


def test_one_unreadable_row_does_not_take_the_profile_down() -> None:
    rows = _prop_accounts_of({"propAccounts": [_row(), {"kind": "eval"}, "junk"]})
    assert [row.id for row in rows] == ["a1"]
    assert _prop_accounts_of({}) == []
