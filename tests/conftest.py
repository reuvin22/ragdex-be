"""Test fixtures.

The suite never touches Firebase or OpenRouter: authentication is overridden
with a fake user and the repositories are patched per test. A test that needs
a network is a test that will fail for reasons unrelated to the code under it.

Note the override style. Settings and identity are replaced through FastAPI's
``dependency_overrides`` rather than by monkeypatching ``get_settings``:
``app/main.py`` binds that name at import, so patching the module it came from
would not reach it. Overriding the dependency reaches every route regardless.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user, get_verified_user
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="development",
        cors_origins=["http://localhost:5173", "http://trades-sable-mu.vercel.app"],
        rate_limit_per_minute=1_000,
        coach_rate_limit_per_minute=1_000,
    )


@pytest.fixture
def verified_user() -> CurrentUser:
    return CurrentUser(
        uid="trader-1",
        email="trader@example.com",
        email_verified=True,
        name="Alex Moreno",
    )


@pytest.fixture
def unverified_user() -> CurrentUser:
    return CurrentUser(
        uid="trader-2", email="new@example.com", email_verified=False, name=""
    )


@pytest.fixture
def app(monkeypatch, settings) -> Iterator[FastAPI]:
    """An app with no credentials and no identity attached.

    Use this directly to test what happens to an anonymous caller; use the
    ``client`` fixture for anything that assumes a signed-in trader.
    """
    # The lifespan is the only thing that reaches for real credentials.
    monkeypatch.setattr("app.main.init_firebase", lambda: None)

    from app.main import create_app

    instance = create_app()
    instance.dependency_overrides[get_settings] = lambda: settings

    yield instance

    instance.dependency_overrides.clear()


@pytest.fixture
def client(app, verified_user) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = lambda: verified_user
    app.dependency_overrides[get_verified_user] = lambda: verified_user

    with TestClient(app) as test_client:
        yield test_client
