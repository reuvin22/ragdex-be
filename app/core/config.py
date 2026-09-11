"""Application settings.

Every value comes from the environment. Nothing here has a usable default for
a secret: a missing key fails loudly at startup rather than quietly running
with something weak, which is the whole point of validating settings once at
import rather than reaching for ``os.environ`` at the call site.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Runtime ---------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    project_name: str = "RagDex API"
    api_v1_prefix: str = "/api/v1"

    # -- Network -----------------------------------------------------------
    # Explicit allowlists, never "*": a credentialed request with a wildcard
    # origin is rejected by browsers anyway, and a wildcard host invites
    # Host-header poisoning behind a proxy.
    # ``NoDecode`` because pydantic-settings JSON-decodes a list field inside
    # the environment source, before any validator runs — so without it a
    # comma-separated value fails to parse and ``_split_csv`` below is never
    # reached, whatever it says it accepts.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"]
    )

    # -- Firebase ----------------------------------------------------------
    # The full service-account JSON on one line. It is a credential with
    # complete access to the project, so it lives only in the environment and
    # is never written to disk or logged.
    firebase_service_account: SecretStr | None = None
    firebase_project_id: str | None = None

    # -- OpenRouter --------------------------------------------------------
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = ""
    openrouter_timeout_seconds: float = 45.0

    # -- Limits ------------------------------------------------------------
    # A journal request is text. Anything larger is a mistake or an attack.
    max_request_bytes: int = 256 * 1024
    rate_limit_per_minute: int = 60
    coach_rate_limit_per_minute: int = 12

    @field_validator("cors_origins", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept either a JSON array or a comma-separated list.

        Platform env editors rarely make JSON pleasant to type.
        """
        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return []
            if trimmed.startswith("["):
                return json.loads(trimmed)
            return [item.strip() for item in trimmed.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def service_account_info(self) -> dict[str, Any] | None:
        """The parsed service account, or None when running without Firebase."""
        if self.firebase_service_account is None:
            return None

        raw = self.firebase_service_account.get_secret_value()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - config error
            raise ValueError("FIREBASE_SERVICE_ACCOUNT is not valid JSON.") from exc

        missing = [
            field
            for field in ("project_id", "client_email", "private_key")
            if not parsed.get(field)
        ]
        if missing:
            raise ValueError(
                "FIREBASE_SERVICE_ACCOUNT is missing: " + ", ".join(missing)
            )
        return parsed


@lru_cache
def get_settings() -> Settings:
    """Settings are read once. The cache is also the seam tests override."""
    return Settings()
