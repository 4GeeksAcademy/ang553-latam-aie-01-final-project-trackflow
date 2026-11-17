"""MCP-facing schemas derived from TrackFlow's real HTTP contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CreateIncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=36)
    incident_date: date
    country: Literal["US", "ES"]
    customer_type: Literal["B2B", "B2C"]
    tracking_number: str = Field(min_length=8, max_length=200)
    carrier: str = Field(min_length=1, max_length=50)
    category: Literal[
        "LOST_PARCEL",
        "DELAYED_DELIVERY",
        "WRONG_ADDRESS",
        "RETURN_REQUEST",
        "DAMAGE",
    ]
    description: str = Field(min_length=5, max_length=5000)
    customer_email: str | None = Field(default=None, max_length=320)
    source_channel: Literal["email", "whatsapp", "phone", "backoffice"]
    source_reference: str | None = Field(default=None, max_length=200)

    @field_validator("customer_email")
    @classmethod
    def validate_customer_email(cls, value: str | None) -> str | None:
        if value is not None and "@" not in value:
            raise ValueError("invalid customer_email")
        return value


class GetIncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(pattern=r"^TRF-\d{6}$")


class UpdateIncidentStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(pattern=r"^TRF-\d{6}$")
    expected_version: int = Field(ge=1, strict=True)
    status: Literal["OPEN", "CLOSED", "DISCARDED"]
    reason: str | None = Field(default=None, max_length=2000)
    satisfaction_score: int | None = Field(default=None, ge=1, le=5, strict=True)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "UpdateIncidentStatusInput":
        if self.status == "CLOSED" and self.satisfaction_score is None:
            raise ValueError("satisfaction_score is required when closing")
        if self.status != "CLOSED" and self.satisfaction_score is not None:
            raise ValueError("satisfaction_score is only valid when closing")
        return self

    def api_payload(self) -> dict[str, object]:
        return self.model_dump(exclude={"incident_id"}, mode="json")


class GetInventoryProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, strict=True)


class GetAuthorizedTrackingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_number: str = Field(min_length=1, max_length=128)

    @field_validator("tracking_number")
    @classmethod
    def normalize_tracking_number(cls, value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("#"):
            normalized = normalized[1:]
        if not normalized or not normalized.replace("-", "").isalnum():
            raise ValueError("tracking_number must be a bounded identifier")
        return normalized


class IncidentProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: str = Field(pattern=r"^TRF-\d{6}$")
    category: Literal[
        "LOST_PARCEL",
        "DELAYED_DELIVERY",
        "WRONG_ADDRESS",
        "RETURN_REQUEST",
        "DAMAGE",
    ]
    status: Literal["OPEN", "CLOSED", "DISCARDED"]
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1, strict=True)


class InventoryProductProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    sku: str
    category: Literal["fashion", "electronics", "cosmetics"]
    warehouse: Literal["LA", "ZGZ"]
    current_stock: int


class AuthorizedTrackingProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["found", "not_found", "unavailable"]
    tracking_number: str | None = Field(default=None, min_length=1, max_length=128)
    dispatch_state: Literal["recorded_dispatch"] | None = None
    commercial_order_id: UUID | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "AuthorizedTrackingProjection":
        if self.status == "found":
            if (
                self.tracking_number is None
                or self.dispatch_state != "recorded_dispatch"
                or self.commercial_order_id is None
            ):
                raise ValueError("found tracking requires its safe dispatch projection")
        elif any(
            value is not None
            for value in (
                self.tracking_number,
                self.dispatch_state,
                self.commercial_order_id,
            )
        ):
            raise ValueError("non-found tracking must not expose order data")
        return self
