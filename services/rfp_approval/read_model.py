"""Safe read models for a human reviewing one departmental revision."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import SafePreviewArtifactPayload
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_artifacts,
    rfp_approval_validation_bundles,
)
from services.rfp_approval.store import ApprovalStoreError


def list_department_review_inbox(
    session: Session,
    *,
    department: str,
    environment: str,
) -> list[dict[str, Any]]:
    """List only current actionable revisions for one authorized department."""

    revision = rfp_approval_department_revisions
    rows = session.execute(
        select(
            rfp_approval_runs.c.approval_run_id,
            rfp_approval_runs.c.ticket_id,
            revision.c.revision_id,
            revision.c.department_id,
            revision.c.review_revision_no,
            revision.c.status,
            revision.c.content_hash,
            revision.c.validation_bundle_id,
            revision.c.created_at,
        )
        .select_from(
            rfp_approval_runs.join(
                revision,
                and_(
                    revision.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    revision.c.ticket_id == rfp_approval_runs.c.ticket_id,
                ),
            )
        )
        .where(
            rfp_approval_runs.c.environment == environment,
            rfp_approval_runs.c.status == "active",
            revision.c.department_id == department,
            revision.c.is_current.is_(True),
            revision.c.status == "pending_approval",
        )
        .order_by(revision.c.created_at, revision.c.revision_id)
    ).mappings().all()
    return [
        {
            "approval_run_id": row["approval_run_id"],
            "ticket_id": row["ticket_id"],
            "revision_id": row["revision_id"],
            "department": DepartmentId(row["department_id"]),
            "review_revision_no": row["review_revision_no"],
            "status": row["status"],
            "content_hash": row["content_hash"],
            "validation_bundle_id": row["validation_bundle_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_department_review(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    revision_id: UUID,
    department: str,
) -> dict[str, Any]:
    """Return only the validated safe preview and non-secret review metadata."""

    revision = rfp_approval_department_revisions
    bundle = rfp_approval_validation_bundles
    artifact = rfp_approval_validation_artifacts
    draft = rfp_approval_corrected_drafts
    row = session.execute(
        select(
            rfp_approval_runs.c.status.label("run_status"),
            revision.c.status.label("revision_status"),
            revision.c.is_current,
            revision.c.state_version,
            revision.c.content_hash,
            revision.c.validation_payload,
            revision.c.content_payload,
            bundle.c.validation_bundle_id,
            bundle.c.validated_at,
            artifact.c.artifact_id.label("safe_preview_artifact_id"),
            artifact.c.payload.label("safe_preview_payload"),
            artifact.c.payload_hash.label("safe_preview_payload_hash"),
            draft.c.provenance.label("draft_provenance"),
        )
        .select_from(
            rfp_approval_runs.join(
                revision,
                and_(
                    revision.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    revision.c.ticket_id == rfp_approval_runs.c.ticket_id,
                ),
            )
            .join(
                bundle,
                bundle.c.validation_bundle_id == revision.c.validation_bundle_id,
            )
            .join(
                artifact,
                artifact.c.artifact_id == bundle.c.safe_preview_artifact_id,
            )
            .join(
                draft,
                draft.c.draft_artifact_id == bundle.c.corrected_draft_id,
            )
        )
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            revision.c.revision_id == revision_id,
            revision.c.department_id == department,
            artifact.c.artifact_type == "safe_preview",
        )
    ).mappings().one_or_none()
    if row is None:
        raise ApprovalStoreError("review_target_not_found")
    values = dict(row)
    try:
        preview = SafePreviewArtifactPayload.model_validate_json(
            json.dumps(values["safe_preview_payload"])
        )
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("safe_preview_invalid") from error
    if preview.gate_status != "passed" or not preview.rendered_content:
        raise ApprovalStoreError("safe_preview_not_displayable")
    content_payload = values["content_payload"]
    evidence = content_payload.get("evidence", []) if isinstance(content_payload, dict) else []
    pending_commercial_items = (
        content_payload.get("pending_commercial_items", [])
        if isinstance(content_payload, dict)
        else []
    )
    return {
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "revision_id": revision_id,
        "department": DepartmentId(department),
        "run_status": values["run_status"],
        "revision_status": values["revision_status"],
        "is_current": values["is_current"],
        "state_version": values["state_version"],
        "content_hash": values["content_hash"],
        "validation_bundle_id": values["validation_bundle_id"],
        "validated_at": values["validated_at"],
        "validation": values["validation_payload"],
        "pending_commercial_items": pending_commercial_items,
        "safe_preview_artifact_id": values["safe_preview_artifact_id"],
        "safe_preview_payload_hash": values["safe_preview_payload_hash"],
        "safe_preview": preview.rendered_content,
        "evidence": evidence,
        "provenance": values["draft_provenance"],
        "allowed_actions": ["approve", "reject", "request_changes"],
    }
