"""
TrackFlow Auth models — Pydantic v2.

Separate models for persistence, request, and response to prevent
``hashed_password`` from leaking into HTTP responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Role enum ────────────────────────────────────────────────────────────────


class Role(str, Enum):
    admin = "admin"
    manager = "manager"
    user = "user"


# ── Internal / persistence models ────────────────────────────────────────────
# These carry ``hashed_password`` and are never returned from API endpoints.


class UserInDB(BaseModel):
    """Internal representation of a User, stored in TinyDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    hashed_password: str
    is_active: bool = True
    role: Role = Role.user
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class ProfileInDB(BaseModel):
    """Internal representation of a Profile, stored in TinyDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    name: str | None = None
    phone: str | None = None
    address: str | None = None


# ── Request models ───────────────────────────────────────────────────────────


class UserCreate(BaseModel):
    """Payload for creating a new user — credentials only."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str = Field(min_length=8)


class UserRegister(BaseModel):
    """Payload for public registration — credentials + optional profile data."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str = Field(min_length=8)
    name: str | None = None
    phone: str | None = None
    address: str | None = None


class UserUpdate(BaseModel):
    """Payload for updating user fields."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    password: str | None = Field(default=None, min_length=8)
    is_active: bool | None = None
    role: Role | None = None


class ProfileCreate(BaseModel):
    """Payload for creating a profile."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    name: str | None = None
    phone: str | None = None
    address: str | None = None


class ProfileUpdate(BaseModel):
    """Payload for updating a profile."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    phone: str | None = None
    address: str | None = None


# ── Response models ──────────────────────────────────────────────────────────
# These NEVER expose ``hashed_password``.


class UserResponse(BaseModel):
    """Public representation of a User — safe for HTTP responses."""

    id: str
    email: str
    is_active: bool
    role: Role
    created_at: str


class ProfileResponse(BaseModel):
    """Public representation of a Profile — safe for HTTP responses."""

    id: str
    user_id: str
    name: str | None = None
    phone: str | None = None
    address: str | None = None


# ── Password-reset token model ──────────────────────────────────────────────


class PasswordResetTokenInDB(BaseModel):
    """Persisted record of an issued password-reset token.

    Stores a SHA-256 hash of the JWT ``jti`` claim rather than the raw JWT.
    This allows checking single-use semantics without exposing the token itself.
    """

    jti_hash: str  # SHA-256 of the JWT ``jti`` claim
    user_id: str  # TinyDB user id the token was issued for
    expires_at: str  # ISO-8601 datetime (UTC)
    used: bool = False  # Whether the token has been consumed
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ── Request models for password-reset flow ──────────────────────────────────


class ForgotPasswordRequest(BaseModel):
    """Payload for POST /auth/forgot-password."""

    model_config = ConfigDict(extra="forbid")

    email: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class ResetPasswordRequest(BaseModel):
    """Payload for POST /auth/reset-password."""

    model_config = ConfigDict(extra="forbid")

    token: str
    new_password: str = Field(min_length=8)


# ── Request model for change-password ────────────────────────────────────────


class ChangePasswordRequest(BaseModel):
    """Payload for POST /auth/change-password.

    Authenticated users provide their current password and a new password.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str = Field(min_length=8)


# ── Generic response models ─────────────────────────────────────────────────


class MessageResponse(BaseModel):
    """Generic message response — safe for public endpoints."""

    message: str