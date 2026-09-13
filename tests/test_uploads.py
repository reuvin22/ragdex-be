"""Signed upload slots.

This route never sees the bytes, so everything it is responsible for happens
before a single one is sent: what may be uploaded, how big, under what name,
and whose it is. Each of those is a test here, because a mistake in any of them
is not a broken feature — it is an open bucket.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from app.core.config import Settings
from app.core.errors import AppError
from app.services import r2
from pydantic import SecretStr


def _settings(**overrides) -> Settings:
    return Settings(
        r2_account_id="acct123",
        r2_access_key_id="AKIAEXAMPLE",
        r2_secret_access_key=SecretStr("shhh"),
        r2_bucket="ragdex-images",
        **overrides,
    )


def _query(url: str) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(urlparse(url).query).items()}


# -- the signature ---------------------------------------------------------


def test_a_put_url_carries_a_complete_signature() -> None:
    url = r2.presign_put(
        key="messages/u1/abc.jpg",
        content_type="image/jpeg",
        content_length=2_048,
        settings=_settings(),
    )
    query = _query(url)

    assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert query["X-Amz-Credential"].startswith("AKIAEXAMPLE/")
    assert query["X-Amz-Credential"].endswith("/auto/s3/aws4_request")
    assert len(query["X-Amz-Signature"]) == 64
    assert query["X-Amz-Expires"] == "600"


def test_the_url_points_at_the_bucket_and_key() -> None:
    url = r2.presign_put(
        key="charts/u1/abc.png",
        content_type="image/png",
        content_length=10,
        settings=_settings(),
    )
    parts = urlparse(url)

    assert parts.hostname == "acct123.r2.cloudflarestorage.com"
    assert parts.path == "/ragdex-images/charts/u1/abc.png"


def test_size_and_type_are_both_signed() -> None:
    """The cap is only real if the signature binds it. Signed headers the
    client must reproduce exactly are what make a URL cut for 2MB useless for
    900MB."""
    query = _query(
        r2.presign_put(
            key="messages/u1/abc.jpg",
            content_type="image/jpeg",
            content_length=2_048,
            settings=_settings(),
        )
    )

    assert query["X-Amz-SignedHeaders"] == "content-length;content-type;host"


def test_a_different_size_is_a_different_signature() -> None:
    def signature(length: int) -> str:
        return _query(
            r2.presign_put(
                key="messages/u1/abc.jpg",
                content_type="image/jpeg",
                content_length=length,
                settings=_settings(),
            )
        )["X-Amz-Signature"]

    assert signature(2_048) != signature(900_000_000)


def test_a_read_url_signs_only_the_host() -> None:
    """Nothing to bind on the way out: there is no body and no content type."""
    query = _query(r2.presign_get(key="messages/u1/abc.jpg", settings=_settings()))

    assert query["X-Amz-SignedHeaders"] == "host"
    assert len(query["X-Amz-Signature"]) == 64


def test_the_secret_never_reaches_the_url() -> None:
    url = r2.presign_get(key="messages/u1/abc.jpg", settings=_settings())

    assert "shhh" not in url


def test_unconfigured_storage_says_so_rather_than_signing_nonsense() -> None:
    with pytest.raises(AppError) as caught:
        r2.presign_get(key="messages/u1/abc.jpg", settings=Settings())

    assert caught.value.status_code == 501


# -- keys and ownership ----------------------------------------------------


def test_a_key_is_folder_then_owner() -> None:
    key = r2.object_key("trader-1", "messages", "image/png")

    assert key.startswith("messages/trader-1/")
    assert key.endswith(".png")


def test_keys_do_not_collide() -> None:
    made = {r2.object_key("u1", "messages", "image/png") for _ in range(50)}

    assert len(made) == 50


def test_encrypted_bytes_get_a_neutral_extension() -> None:
    """A chat image is ciphertext by the time it is uploaded, so calling it a
    .png would be a lie that something downstream might act on."""
    assert r2.object_key("u1", "messages", "application/octet-stream").endswith(".bin")


def test_ownership_is_read_off_the_key() -> None:
    assert r2.owns("u1", "messages/u1/abc.jpg")
    assert not r2.owns("u1", "messages/u2/abc.jpg")
    # A uid that merely starts the same is not the same uid.
    assert not r2.owns("u1", "messages/u12/abc.jpg")


def test_a_folder_outside_the_four_is_not_ours() -> None:
    """Whatever that key is, this service did not mint it."""
    assert not r2.owns("u1", "exports/u1/abc.jpg")


def test_the_uid_must_be_the_second_segment() -> None:
    """Not merely present. A prefix or substring match would let a nested path
    smuggle the uid somewhere it means nothing."""
    assert not r2.owns("u1", "messages/u2/u1/abc.jpg")


def test_traversal_is_refused_rather_than_repaired() -> None:
    assert not r2.owns("u1", "messages/u1/../u2/abc.jpg")


# -- the route -------------------------------------------------------------


@pytest.fixture
def storage(app, settings):
    """An app whose settings have R2 configured."""
    from app.core.config import get_settings

    configured = _settings(
        environment=settings.environment,
        cors_origins=settings.cors_origins,
        rate_limit_per_minute=settings.rate_limit_per_minute,
        coach_rate_limit_per_minute=settings.coach_rate_limit_per_minute,
    )
    app.dependency_overrides[get_settings] = lambda: configured
    return app


def test_a_slot_is_scoped_to_the_signed_in_trader(
    client, storage, verified_user
) -> None:
    response = client.post(
        "/api/v1/uploads",
        json={"kind": "messages", "content_type": "image/jpeg", "content_length": 1_000},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key"].startswith(f"messages/{verified_user.uid}/")
    assert "X-Amz-Signature" in body["url"]


def test_an_oversized_image_is_refused_before_it_is_sent(client, storage) -> None:
    response = client.post(
        "/api/v1/uploads",
        json={
            "kind": "messages",
            "content_type": "image/jpeg",
            "content_length": 11 * 1024 * 1024,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "too_large"


def test_a_type_off_the_allowlist_is_refused(client, storage) -> None:
    """SVG is the one that matters: it is a script-delivery format wearing an
    image's name."""
    response = client.post(
        "/api/v1/uploads",
        json={
            "kind": "messages",
            "content_type": "image/svg+xml",
            "content_length": 1_000,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_type"


def test_reading_someone_elses_object_is_forbidden(client, storage) -> None:
    response = client.get(
        "/api/v1/uploads/url", params={"key": "messages/someone-else/a.jpg"}
    )

    assert response.status_code == 403


def test_reading_your_own_object_returns_a_signed_url(
    client, storage, verified_user
) -> None:
    response = client.get(
        "/api/v1/uploads/url",
        params={"key": f"messages/{verified_user.uid}/a.jpg"},
    )

    assert response.status_code == 200
    assert "X-Amz-Signature" in response.json()["url"]


def test_readiness_names_each_missing_r2_setting(app, monkeypatch) -> None:
    """"R2 is not configured" when three of the four are set is the least
    useful thing that endpoint could say."""
    from app.api.v1.routes import health
    from app.core.config import Settings

    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: Settings(r2_account_id="acct123", r2_bucket="ragdex-images"),
    )
    monkeypatch.setattr(
        health, "get_client", lambda: (_ for _ in ()).throw(RuntimeError)
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        checks = test_client.get("/ready").json()["checks"]

    assert "R2_ACCESS_KEY_ID" in checks["images"]
    assert "R2_SECRET_ACCESS_KEY" in checks["images"]
    assert "R2_ACCOUNT_ID" not in checks["images"]
