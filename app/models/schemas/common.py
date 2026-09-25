"""Shapes shared across routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """A plain acknowledgement, for endpoints with nothing else to return."""

    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """Documented so the error envelope appears in the OpenAPI schema rather
    than being something clients discover by hitting it."""

    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    environment: str
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    firestore: bool
    checks: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
