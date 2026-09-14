"""Application settings.

Every value comes from the environment. Nothing here has a usable default for
a secret: a missing key fails loudly at startup rather than quietly running
with something weak, which is the whole point of validating settings once at
import rather than reaching for ``os.environ`` at the call site.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Where a service account file is looked for when nothing names one explicitly.
#
# Render mounts a Secret File at /etc/secrets/<filename> and also at the app
# root, so uploading trading-journal.json in the dashboard is all that is
# needed — no extra environment variable, nothing to keep in step. The local
# entries are for running the service on a workstation.
#
# These are read, never written, and the filenames are matched by .gitignore.
SERVICE_ACCOUNT_PATHS = (
    Path("/etc/secrets/trading-journal.json"),
    Path("/etc/secrets/serviceAccount.json"),
    Path("trading-journal.json"),
    Path("serviceAccount.json"),
)


#: Bridge providers that authenticate with a token.
#:
#: The self-hosted mt5-bridge has no authentication at all, so demanding a
#: token would leave it permanently reported as unconfigured — and its lack of
#: one is a deployment concern (keep it off the internet), not a settings one.
_TOKEN_PROVIDERS = ("metaapi",)


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

    # A path to that same JSON, for hosts that mount secrets as files rather
    # than environment variables. On Render this is a "Secret File": upload
    # trading-journal.json in the dashboard and it appears at
    # /etc/secrets/trading-journal.json.
    #
    # Preferred over the inline variable when both are set. A multi-kilobyte
    # private key pasted into an env var is easy to truncate, easy to mangle by
    # losing the "\n" escapes, and shows up in any process listing that dumps
    # the environment; a file has none of those problems.
    firebase_service_account_file: str | None = None

    firebase_project_id: str | None = None

    # The Web API key, used to reach Identity Toolkit for password sign-in and
    # for the OOB codes behind verification and password reset. The Admin SDK
    # cannot do either: it can mint and verify tokens, but it cannot check a
    # password. This used to sit in the browser bundle; it belongs here.
    firebase_web_api_key: SecretStr | None = None

    # -- Session -----------------------------------------------------------
    # The browser holds an opaque, HttpOnly cookie and nothing else. No ID
    # token, no refresh token, no Firebase config: script on the page cannot
    # read a credential it was never given.
    session_cookie_name: str = "ragdex_session"
    session_days: int = 5
    # Lax is right when the API and the app are same-site, which is what the
    # Vercel rewrite in the client's vercel.json arranges. Cross-site needs
    # "none", and then Safari and Chrome's third-party cookie rules apply.
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # Off only for plain-HTTP local development; any deployment must set it.
    session_cookie_secure: bool = True
    session_cookie_domain: str | None = None

    # -- Google sign-in ----------------------------------------------------
    # Nothing here. The browser runs the Google popup through the Firebase
    # SDK and posts the resulting ID token to /auth/google, which verifies it
    # with the Admin SDK credential this service already holds.
    #
    # The OAuth client id, secret and redirect URI that used to live here are
    # gone with the server-side redirect flow. Firebase provisions and owns
    # its own OAuth client, which is what removes the redirect-URI and
    # cross-project mismatches entirely.

    # Where the browser is sent after sign-in, and the base for links inside
    # emails.
    app_url: str = "https://trades-sable-mu.vercel.app"

    # -- Email -------------------------------------------------------------
    # Brevo sends the branded verification email. Firebase's own template is
    # deliberately not used, so only one message goes out.
    brevo_api_key: SecretStr | None = None
    brevo_sender_email: str = ""
    brevo_sender_name: str = "RagDex"

    # -- OpenRouter --------------------------------------------------------
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = ""
    openrouter_timeout_seconds: float = 45.0
    # Covers the answer AND, on a reasoning model, the thinking that precedes
    # it. The old 1,200 was set when the coach answered in three or four
    # sentences; it now explains a change, what it buys them, and what would
    # undo it, on top of a much longer system prompt.
    #
    # 2,500 still was not enough — the free reasoning models spent the lot
    # thinking and never began. This is the starting budget; openrouter.complete
    # doubles it once on a truncated reply rather than making every request pay
    # for the worst case.
    openrouter_max_tokens: int = 4_000
    # Vision is a different model list: a text model handed a chart answers
    # about the words and silently ignores the picture.
    openrouter_vision_model: str = ""

    # -- Cloudflare R2 -----------------------------------------------------
    # Object storage for images. The browser never sees these: the API signs a
    # short-lived URL and the upload goes straight from the browser to R2, so a
    # ten-megabyte screenshot never passes through this service.
    #
    # The secret is a real credential with write access to the bucket. Treat it
    # like the service account.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str = ""
    #: How long a signed URL stays valid. Long enough to upload 10MB on a poor
    #: connection, short enough that a leaked link is worth little.
    r2_url_ttl_seconds: int = 600

    #: The largest image accepted, before encryption or encoding overhead.
    max_upload_bytes: int = 10 * 1024 * 1024

    # -- Broker sync -------------------------------------------------------
    # Reading a MetaTrader account without asking the trader to install
    # anything means a headless terminal has to run somewhere, logged in as
    # them. Nobody does that for free, so this is the one part of the app
    # with a per-user running cost — and the one most likely to be left
    # unconfigured, which is why /ready names it.
    bridge_provider: str = ""
    bridge_token: SecretStr | None = None
    #: Where the bridge lives. Overridable so a region or a self-hosted
    #: deployment does not need a code change.
    bridge_base_url: str = "https://mt-client-api-v1.new-york.agiliumtrade.ai"
    bridge_provisioning_url: str = (
        "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
    )

    @property
    def bridge_configured(self) -> bool:
        if not self.bridge_provider:
            return False
        if self.bridge_provider in _TOKEN_PROVIDERS:
            return self.bridge_token is not None
        return True

    @property
    def r2_configured(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket
        )

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

    def _service_account_source(self) -> tuple[str, str] | None:
        """Where the credential is coming from, as (description, raw JSON).

        Order matters. An explicitly configured path wins, then the inline
        variable, then the well-known locations — so a deliberate setting is
        never quietly overridden by a file that happens to be lying around.
        """
        if self.firebase_service_account_file:
            path = Path(self.firebase_service_account_file)
            if not path.is_file():
                raise ValueError(
                    f"FIREBASE_SERVICE_ACCOUNT_FILE points at {path}, "
                    "which does not exist."
                )
            return (str(path), path.read_text(encoding="utf-8"))

        if self.firebase_service_account is not None:
            return (
                "FIREBASE_SERVICE_ACCOUNT",
                self.firebase_service_account.get_secret_value(),
            )

        for path in SERVICE_ACCOUNT_PATHS:
            if path.is_file():
                return (str(path), path.read_text(encoding="utf-8"))

        return None

    def service_account_info(self) -> dict[str, Any] | None:
        """The parsed service account, or None when running without Firebase."""
        source = self._service_account_source()
        if source is None:
            return None

        where, raw = source
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - config error
            raise ValueError(f"{where} is not valid JSON.") from exc

        missing = [
            field
            for field in ("project_id", "client_email", "private_key")
            if not parsed.get(field)
        ]
        if missing:
            raise ValueError(f"{where} is missing: " + ", ".join(missing))
        return dict(parsed)


@lru_cache
def get_settings() -> Settings:
    """Settings are read once. The cache is also the seam tests override."""
    return Settings()
