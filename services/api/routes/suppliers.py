"""Supplier directory endpoints for TrackFlow."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from tinydb.table import Document

from services.api.database import suppliers
from services.api.models import (
    Country,
    SupplierCreate,
    SupplierRateUpdate,
    SupplierResponse,
    SupplierStatusUpdate,
    VALID_CATEGORIES,
)

router = APIRouter(prefix="/suppliers", tags=["Suppliers"])

_CATEGORY_PATTERN = "^(" + "|".join(
    re.escape(category) for category in sorted(VALID_CATEGORIES)
) + ")$"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supplier_document_to_response(document: Document) -> SupplierResponse:
    payload = {"id": document.doc_id, **dict(document)}
    return SupplierResponse.model_validate(payload)


@router.post("", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierCreate) -> SupplierResponse:
    supplier_data = payload.model_dump(mode="json", exclude_none=True)
    supplier_data["updated_at"] = _utc_now_iso()

    supplier_id = suppliers.insert(supplier_data)
    created_document = suppliers.get(doc_id=supplier_id)

    if created_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supplier created but could not be retrieved",
        )

    return _supplier_document_to_response(created_document)


@router.get("", response_model=list[SupplierResponse])
def list_suppliers(
    country: Country | None = None,
    category: Annotated[str | None, Query(pattern=_CATEGORY_PATTERN)] = None,
) -> list[SupplierResponse]:
    documents = suppliers.all()

    if country is not None:
        documents = [doc for doc in documents if doc.get("country") == country.value]

    if category is not None:
        documents = [
            doc
            for doc in documents
            if category in (doc.get("categories") or [])
        ]

    return [_supplier_document_to_response(document) for document in documents]


@router.get("/{supplier_id}", response_model=SupplierResponse)
def get_supplier(supplier_id: int) -> SupplierResponse:
    document = suppliers.get(doc_id=supplier_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return _supplier_document_to_response(document)


@router.patch("/{supplier_id}/rate", response_model=SupplierResponse)
def update_supplier_rate(
    supplier_id: int,
    payload: SupplierRateUpdate,
) -> SupplierResponse:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    suppliers.update(
        {
            "rate_per_shipment": payload.rate_per_shipment,
            "updated_at": _utc_now_iso(),
        },
        doc_ids=[supplier_id],
    )

    updated_document = suppliers.get(doc_id=supplier_id)
    if updated_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return _supplier_document_to_response(updated_document)


@router.patch("/{supplier_id}/status", response_model=SupplierResponse)
def update_supplier_status(
    supplier_id: int,
    payload: SupplierStatusUpdate,
) -> SupplierResponse:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    suppliers.update({"status": payload.status.value}, doc_ids=[supplier_id])

    updated_document = suppliers.get(doc_id=supplier_id)
    if updated_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return _supplier_document_to_response(updated_document)


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier(supplier_id: int) -> Response:
    existing_document = suppliers.get(doc_id=supplier_id)
    if existing_document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    suppliers.remove(doc_ids=[supplier_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
