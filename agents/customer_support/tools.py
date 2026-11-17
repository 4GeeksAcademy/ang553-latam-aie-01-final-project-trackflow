"""Typed, read-only operational tools for the TrackFlow support graph."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.inventory_service import (
    InventoryProductNotFound,
    read_inventory_product,
)
from services.incidents.service import IncidentNotFound, read_incident
from services.rag.resilience import QueryDeadline
from agents.customer_support.guardrail_contracts import DispatchState


TOOL_STATEMENT_TIMEOUT_SECONDS = 5.0
SUPPORT_RESPONSE_RESERVE_SECONDS = 3.0


class ToolStatus(str, Enum):
    ok = "ok"
    not_found = "not_found"
    timeout = "timeout"
    unavailable = "unavailable"


@dataclass(frozen=True)
class TicketLookupInput:
    incident_id: str


@dataclass(frozen=True)
class TicketLookupResult:
    status: ToolStatus
    incident_id: str | None = None
    incident_status: str | None = None
    category: str | None = None
    version: int | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class InventoryLookupInput:
    product_id: int

    def __post_init__(self) -> None:
        if self.product_id <= 0:
            raise ValueError("product_id must be positive")


@dataclass(frozen=True)
class InventoryLookupResult:
    status: ToolStatus
    product_id: int | None = None
    name: str | None = None
    sku: str | None = None
    warehouse: str | None = None
    current_stock: int | None = None


@dataclass(frozen=True)
class TrackingLookupInput:
    tracking_number: str

    def __post_init__(self) -> None:
        normalized = self.tracking_number.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", normalized):
            raise ValueError("tracking_number must be a bounded identifier")
        object.__setattr__(self, "tracking_number", normalized)


@dataclass(frozen=True)
class TrackingLookupResult:
    status: ToolStatus
    tracking_number: str | None = None
    dispatch_state: DispatchState | None = None
    commercial_order_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.status is ToolStatus.ok:
            if self.tracking_number is None:
                raise ValueError("successful tracking lookup requires tracking_number")
            TrackingLookupInput(self.tracking_number)
            if self.dispatch_state is not DispatchState.recorded_dispatch:
                raise ValueError("successful tracking lookup requires recorded_dispatch")
            if self.commercial_order_id is None:
                raise ValueError("successful tracking lookup requires commercial_order_id")
            return
        if any(
            value is not None
            for value in (
                self.tracking_number,
                self.dispatch_state,
                self.commercial_order_id,
            )
        ):
            raise ValueError("unsuccessful tracking lookup must not expose order data")


def _is_statement_timeout(error: DBAPIError) -> bool:
    original = getattr(error, "orig", None)
    return getattr(original, "sqlstate", None) == "57014" or getattr(
        original, "pgcode", None
    ) == "57014"


def _rollback_read_transaction(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError:
        pass


def apply_statement_timeout(session: Session, deadline: QueryDeadline) -> int:
    """Apply a transaction-local PostgreSQL timeout bounded by remaining time."""
    available = deadline.remaining() - SUPPORT_RESPONSE_RESERVE_SECONDS
    if available <= 0:
        raise TimeoutError("support query deadline exhausted")
    milliseconds = max(
        1,
        int(min(TOOL_STATEMENT_TIMEOUT_SECONDS, available) * 1000),
    )
    bind = session.get_bind()
    if getattr(bind.dialect, "name", None) == "postgresql":
        session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{milliseconds}ms"},
        )
    return milliseconds


def lookup_ticket(
    session: Session,
    payload: TicketLookupInput,
    access: AccessContext,
    deadline: QueryDeadline,
) -> TicketLookupResult:
    """Read an authorized incident and project only agent-safe fields."""
    try:
        apply_statement_timeout(session, deadline)
        incident = read_incident(session, payload.incident_id, access)
    except IncidentNotFound:
        return TicketLookupResult(status=ToolStatus.not_found)
    except TimeoutError:
        return TicketLookupResult(status=ToolStatus.timeout)
    except DBAPIError as exc:
        timed_out = _is_statement_timeout(exc)
        _rollback_read_transaction(session)
        return TicketLookupResult(
            status=ToolStatus.timeout if timed_out else ToolStatus.unavailable
        )
    except SQLAlchemyError:
        _rollback_read_transaction(session)
        return TicketLookupResult(status=ToolStatus.unavailable)
    except Exception:
        _rollback_read_transaction(session)
        return TicketLookupResult(status=ToolStatus.unavailable)
    return TicketLookupResult(
        status=ToolStatus.ok,
        incident_id=incident.incident_id,
        incident_status=incident.status,
        category=incident.category,
        version=incident.version,
        updated_at=incident.updated_at,
    )


def lookup_inventory_product(
    session: Session,
    payload: InventoryLookupInput,
    access: AccessContext,
    deadline: QueryDeadline,
) -> InventoryLookupResult:
    """Read an authorized product through the shared inventory service."""
    try:
        apply_statement_timeout(session, deadline)
        product = read_inventory_product(
            session,
            product_id=payload.product_id,
            access=access,
        )
    except InventoryProductNotFound:
        return InventoryLookupResult(status=ToolStatus.not_found)
    except TimeoutError:
        return InventoryLookupResult(status=ToolStatus.timeout)
    except DBAPIError as exc:
        timed_out = _is_statement_timeout(exc)
        _rollback_read_transaction(session)
        return InventoryLookupResult(
            status=ToolStatus.timeout if timed_out else ToolStatus.unavailable
        )
    except SQLAlchemyError:
        _rollback_read_transaction(session)
        return InventoryLookupResult(status=ToolStatus.unavailable)
    except Exception:
        _rollback_read_transaction(session)
        return InventoryLookupResult(status=ToolStatus.unavailable)
    return InventoryLookupResult(
        status=ToolStatus.ok,
        product_id=product.id,
        name=product.name,
        sku=product.sku,
        warehouse=product.warehouse.value,
        current_stock=product.current_stock,
    )
