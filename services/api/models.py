"""
TrackFlow Supplier models — Pydantic v2.

Based strictly on the TrackFlow CONTEXT.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ── Constants ────────────────────────────────────────────────────────────────


class Country(str, Enum):
    USA = "USA"
    Spain = "Spain"


class SupplierStatus(str, Enum):
    active = "active"
    suspended = "suspended"


VALID_CATEGORIES: frozenset[str] = frozenset({
    "carrier_last_mile",
    "carrier_international",
    "warehouse_supplies",
    "packaging_materials",
    "reverse_logistics",
    "fleet_maintenance",
    "it_and_wms_software",
    "cleaning_and_facilities",
})


# ── Mapping: country → valid currency ────────────────────────────────────────

_COUNTRY_CURRENCY: dict[str, str] = {
    "USA": "USD",
    "Spain": "EUR",
}


# ── Create model ─────────────────────────────────────────────────────────────


class SupplierCreate(BaseModel):
    name: str
    country: Country
    categories: list[str]
    rate_per_shipment: float = Field(gt=0)
    currency: Literal["USD", "EUR"]
    status: SupplierStatus
    service_zone: str | None = None
    contact_email: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_categories(self) -> "SupplierCreate":
        if not self.categories:
            raise ValueError("At least one category is required")
        for cat in self.categories:
            if cat not in VALID_CATEGORIES:
                raise ValueError(
                    f"Invalid category '{cat}'. "
                    f"Valid categories: {sorted(VALID_CATEGORIES)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_country_currency(self) -> "SupplierCreate":
        expected = _COUNTRY_CURRENCY.get(self.country.value)
        if expected is None:
            raise ValueError(f"Unsupported country: {self.country}")
        if self.currency != expected:
            raise ValueError(
                f"Currency '{self.currency}' is not valid for {self.country.value}. "
                f"{self.country.value} must use '{expected}'."
            )
        return self


# ── Response model ───────────────────────────────────────────────────────────


class SupplierResponse(BaseModel):
    id: int
    name: str
    country: Country
    categories: list[str]
    rate_per_shipment: float
    currency: Literal["USD", "EUR"]
    status: SupplierStatus
    updated_at: datetime
    service_zone: str | None = None
    contact_email: str | None = None
    notes: str | None = None


# ── Partial-update models ────────────────────────────────────────────────────


class SupplierRateUpdate(BaseModel):
    rate_per_shipment: float = Field(gt=0)


class SupplierStatusUpdate(BaseModel):
    status: SupplierStatus