"""Typed operational incident contracts, distinct from CSV aggregate contracts."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scripts.incidents import (
    INCIDENT_ID_PATTERN,
    VALID_CATEGORIES,
    VALID_COUNTRIES,
    VALID_CUSTOMER_TYPES,
    VALID_STATUSES,
)


class SourceChannel(str, Enum):
    email = "email"
    whatsapp = "whatsapp"
    phone = "phone"
    backoffice = "backoffice"


class ClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(min_length=1, max_length=36)
    display_name: str = Field(min_length=1, max_length=200)


class ClientResponse(BaseModel):
    client_id: str
    display_name: str
    is_active: bool = True


class MembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=36)
    can_view_sensitive: bool = False


class MembershipResponse(BaseModel):
    user_id: str
    client_id: str
    can_view_sensitive: bool


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=36)
    incident_date: date
    country: str
    customer_type: str
    tracking_number: str = Field(min_length=8, max_length=200)
    carrier: str = Field(min_length=1, max_length=50)
    category: str
    description: str = Field(min_length=5, max_length=5000)
    customer_email: str | None = Field(default=None, max_length=320)
    source_channel: SourceChannel
    source_reference: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_taxonomy(self) -> IncidentCreate:
        if self.country not in VALID_COUNTRIES:
            raise ValueError("invalid country")
        if self.customer_type not in VALID_CUSTOMER_TYPES:
            raise ValueError("invalid customer_type")
        if self.category not in VALID_CATEGORIES:
            raise ValueError("invalid category")
        if self.customer_email is not None and "@" not in self.customer_email:
            raise ValueError("invalid customer_email")
        return self


class IncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    status: str
    reason: str | None = Field(default=None, max_length=2000)
    satisfaction_score: int | None = Field(default=None, ge=1, le=5, strict=True)

    @model_validator(mode="after")
    def validate_update(self) -> IncidentUpdate:
        if self.status not in VALID_STATUSES:
            raise ValueError("invalid status")
        if self.status == "CLOSED" and self.satisfaction_score is None:
            raise ValueError("satisfaction_score is required when closing")
        if self.status != "CLOSED" and self.satisfaction_score is not None:
            raise ValueError("satisfaction_score is only valid when closing")
        return self


class IncidentHistoryResponse(BaseModel):
    version: int
    event_type: str
    previous_status: str | None
    new_status: str
    reason: str | None
    occurred_at: datetime


class IncidentResponse(BaseModel):
    incident_id: str
    incident_date: date
    country: str
    customer_type: str
    tracking_number: str
    carrier: str
    category: str
    description: str
    status: str
    customer_email: str | None
    source_channel: SourceChannel
    created_at: datetime
    updated_at: datetime
    version: int
    history: list[IncidentHistoryResponse]

    @field_validator("incident_id")
    @classmethod
    def validate_incident_id(cls, value: str) -> str:
        if not INCIDENT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid incident_id")
        return value
