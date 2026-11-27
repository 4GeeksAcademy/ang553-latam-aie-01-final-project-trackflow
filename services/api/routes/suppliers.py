"""Supplier directory endpoints for TrackFlow."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from tinydb.table import Document

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.database import suppliers
from services.api.models import (
    Country,
    SupplierCreate,
    SupplierCreatedResponse,
    SupplierMutationResponse,
    SupplierRateUpdate,
    SupplierResponse,
    SupplierStatusUpdate,
    VALID_CATEGORIES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/suppliers", tags=["Suppliers"])

_OPERATOR_ROLES = frozenset({Role.admin, Role.manager})
_DIRECTORY_FIELDS = ("name", "country", "categories", "status", "updated_at", "service_zone")
_COMMERCIAL_FIELDS = ("rate_per_shipment", "currency", "contact_email", "notes")

_CATEGORY_PATTERN = "^(" + "|".join(
    re.escape(category) for category in sorted(VALID_CATEGORIES)
) + ")$"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supplier_document_to_response(document: Document, role: Role) -> SupplierResponse:
    payload = {"id": document.doc_id, **{field: document.get(field) for field in _DIRECTORY_FIELDS}}
    if role in _OPERATOR_ROLES:
        payload.update({field: document.get(field) for field in _COMMERCIAL_FIELDS})
    return SupplierResponse.model_validate(payload)


def _require_catalog_operator(user: Annotated[UserInDB, Depends(get_current_user)]) -> None:
    if user.role not in _OPERATOR_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin or manager role required")


def _require_catalog_admin(user: Annotated[UserInDB, Depends(get_current_user)]) -> None:
    if user.role != Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")


@router.post(
    "", response_model=SupplierCreatedResponse, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_catalog_operator)],
)
def create_supplier(payload: SupplierCreate) -> SupplierCreatedResponse:
    supplier_data = payload.model_dump(mode="json", exclude_none=True)
    supplier_data["updated_at"] = _utc_now_iso()

    try:
        supplier_id = suppliers.insert(supplier_data)
    except Exception:
        logger.exception("Failed to insert supplier")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to complete the supplier operation. Please try again.",
        )
    created_document = suppliers.get(doc_id=supplier_id)

    if created_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supplier created but could not be retrieved",
        )

    return SupplierCreatedResponse(id=created_document.doc_id)


@router.get("", response_model=list[SupplierResponse], response_model_exclude_unset=True)
def list_suppliers(
    user: Annotated[UserInDB, Depends(get_current_user)],
    response: Response,
    country: Country | None = None,
    category: Annotated[str | None, Query(pattern=_CATEGORY_PATTERN)] = None,
) -> list[SupplierResponse]:
    response.headers["Cache-Control"] = "private, no-store"
    documents = suppliers.all()

    if country is not None:
        documents = [doc for doc in documents if doc.get("country") == country.value]

    if category is not None:
        documents = [
            doc
            for doc in documents
            if category in (doc.get("categories") or [])
        ]

    return [_supplier_document_to_response(document, user.role) for document in documents]


@router.get("/{supplier_id}", response_model=SupplierResponse, response_model_exclude_unset=True)
def get_supplier(
    supplier_id: int,
    user: Annotated[UserInDB, Depends(get_current_user)],
    response: Response,
) -> SupplierResponse:
    document = suppliers.get(doc_id=supplier_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    response.headers["Cache-Control"] = "private, no-store"
    return _supplier_document_to_response(document, user.role)


@router.patch(
    "/{supplier_id}/rate", response_model=SupplierMutationResponse,
    dependencies=[Depends(_require_catalog_operator)],
)
def update_supplier_rate(
    supplier_id: int,
    payload: SupplierRateUpdate,
) -> SupplierMutationResponse:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    try:
        suppliers.update(
            {
                "rate_per_shipment": payload.rate_per_shipment,
                "updated_at": _utc_now_iso(),
            },
            doc_ids=[supplier_id],
        )
    except Exception:
        logger.exception("Failed to update supplier rate (id=%s)", supplier_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to complete the supplier operation. Please try again.",
        )

    updated_document = suppliers.get(doc_id=supplier_id)
    if updated_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return SupplierMutationResponse(
        id=updated_document.doc_id,
        updated_at=updated_document["updated_at"],
    )


@router.patch(
    "/{supplier_id}/status", response_model=SupplierMutationResponse,
    dependencies=[Depends(_require_catalog_operator)],
)
def update_supplier_status(
    supplier_id: int,
    payload: SupplierStatusUpdate,
) -> SupplierMutationResponse:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    try:
        suppliers.update(
            {
                "status": payload.status.value,
                "updated_at": _utc_now_iso(),
            },
            doc_ids=[supplier_id],
        )
    except Exception:
        logger.exception("Failed to update supplier status (id=%s)", supplier_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to complete the supplier operation. Please try again.",
        )

    updated_document = suppliers.get(doc_id=supplier_id)
    if updated_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return SupplierMutationResponse(
        id=updated_document.doc_id,
        updated_at=updated_document["updated_at"],
    )


@router.delete(
    "/{supplier_id}",
    response_class=Response,
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_catalog_admin)],
)
def delete_supplier(supplier_id: int) -> Response:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    try:
        suppliers.remove(doc_ids=[supplier_id])
    except Exception:
        logger.exception("Failed to remove supplier (id=%s)", supplier_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to complete the supplier operation. Please try again.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
