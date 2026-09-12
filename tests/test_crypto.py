"""Application-level encryption: what it hides, and what it must not break."""

from __future__ import annotations

import base64
import os
from decimal import Decimal

import pytest
from app.core.crypto import (
    EncryptionError,
    seal,
    seal_fields,
    unseal,
    unseal_fields,
)

_SOME_FIELDS = frozenset({"rationale", "netPl"})


@pytest.fixture
def key(monkeypatch) -> str:
    value = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", value)
    return value


def test_a_sealed_value_does_not_contain_the_original(key) -> None:
    sealed = seal("Revenge traded the open again")

    assert "Revenge" not in sealed
    assert sealed.startswith("enc.v1.")
    assert unseal(sealed) == "Revenge traded the open again"


def test_types_survive_the_round_trip(key) -> None:
    """JSON-encoded before sealing, so a number comes back a number. Sealing the
    string form and hoping the caller re-parses is how types quietly rot."""
    for value in (42, 3.5, True, ["a", "b"], {"k": "v"}, "text"):
        assert unseal(seal(value)) == value


def test_none_stays_none(key) -> None:
    """An absent field must stay absent, not become an opaque blob that every
    `is None` check downstream then misses."""
    assert seal(None) is None
    assert unseal(None) is None


def test_plaintext_reads_straight_through(key) -> None:
    """What makes this safe to switch on against a database that already holds
    data: documents written before the key existed keep working."""
    assert unseal("written before encryption") == "written before encryption"
    assert unseal(1234) == 1234


def test_a_tampered_value_is_refused(key) -> None:
    """AES-GCM is authenticated, so an edited ciphertext is rejected rather than
    decrypting to rubbish that would then be shown to somebody."""
    sealed = seal("original")
    tampered = sealed[:-4] + "AAAA"

    with pytest.raises(EncryptionError):
        unseal(tampered)


def test_the_wrong_key_cannot_read_it(key, monkeypatch) -> None:
    sealed = seal("private note")
    monkeypatch.setenv(
        "DATA_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )

    with pytest.raises(EncryptionError):
        unseal(sealed)


def test_sealed_data_without_a_key_raises_rather_than_leaking(key, monkeypatch) -> None:
    """Showing ciphertext as though it were the person's own note would be
    worse than an error."""
    sealed = seal("private note")
    monkeypatch.delenv("DATA_ENCRYPTION_KEY")

    with pytest.raises(EncryptionError):
        unseal(sealed)


def test_without_a_key_values_pass_through(monkeypatch) -> None:
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    assert seal("plain") == "plain"


def test_only_the_named_fields_are_sealed(key) -> None:
    """The fields Firestore filters and orders on have to stay readable to
    Firestore, or listing and paging stop working."""
    document = {
        "uid": "trader-1",
        "createdAt": "2026-01-01",
        "rationale": "Chased the open",
        "netPl": Decimal("-180.5"),
    }

    stored = seal_fields(document, _SOME_FIELDS)

    assert stored["uid"] == "trader-1"
    assert stored["createdAt"] == "2026-01-01"
    assert stored["rationale"].startswith("enc.v1.")
    assert "Chased" not in str(stored)

    back = unseal_fields(stored, _SOME_FIELDS)
    assert back["rationale"] == "Chased the open"
    assert back["uid"] == "trader-1"
