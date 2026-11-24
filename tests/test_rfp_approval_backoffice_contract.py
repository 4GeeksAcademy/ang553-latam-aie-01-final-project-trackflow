from pathlib import Path


BACKOFFICE = Path(__file__).parents[1] / "uis" / "backoffice"


def test_backoffice_exposes_authenticated_department_review_flow() -> None:
    page = (BACKOFFICE / "app/backoffice/rfps/page.tsx").read_text(encoding="utf-8")
    component = (
        BACKOFFICE / "components/rfp/RfpApprovalInbox.tsx"
    ).read_text(encoding="utf-8")
    api = (BACKOFFICE / "lib/rfpApi.ts").read_text(encoding="utf-8")
    types = (BACKOFFICE / "types/rfp.ts").read_text(encoding="utf-8")

    assert 'import { RfpApprovalInbox }' in page
    assert "if (!isAdmin) return <RfpApprovalInbox />" in page
    assert page.count("<RfpApprovalInbox />") == 2
    assert "listRfpApprovalInbox" in component
    assert "getRfpDepartmentReview" in component
    assert "submitRfpApprovalDecision" in component
    assert 'request("/rfps/approval/inbox"' in api
    assert "/revisions/${encodeURIComponent(item.revision_id)}/review" in api
    assert "/revisions/${encodeURIComponent(item.revision_id)}/decision" in api
    assert "acknowledge_pending_commercial_items" in api
    assert "pending_commercial_items" in component
    assert 'export type RfpApprovalAction = "approve" | "reject" | "request_changes"' in types
    assert '| "esperando_aprobación"' in types
    assert '| "terminado"' in types
    assert 'esperando_aprobación: "Esperando aprobación"' in page
    assert 'terminado: "Terminado"' in page
    assert '"esperando_aprobación",' in api
    assert '"terminado",' in api


def test_authenticated_approvers_can_navigate_to_the_rfp_inbox() -> None:
    header = (
        BACKOFFICE / "components/layout/BackofficeHeader.tsx"
    ).read_text(encoding="utf-8")
    rfp_link = header[header.index('href="/backoffice/rfps"') - 80 :]

    assert "{user && (" in rfp_link[:80]
    assert 'href="/backoffice/rfps"' in rfp_link
    assert 'user?.role === "admin"' not in rfp_link.split("</Link>", 1)[0]


def test_backoffice_requires_evidence_acknowledgement_and_reasons() -> None:
    component = (
        BACKOFFICE / "components/rfp/RfpApprovalInbox.tsx"
    ).read_text(encoding="utf-8")

    assert "review.safe_preview" in component
    assert "review.evidence" in component
    assert "review.provenance" in component
    assert "!acknowledged" in component
    assert 'action !== "approve" && !reason.trim()' in component
    assert "Confirmo que revisé el contenido, su evidencia y procedencia" in component
    assert "principal_id" not in component
    assert "authorization_id" not in component
    assert "principal_id" not in (
        BACKOFFICE / "lib/rfpApi.ts"
    ).read_text(encoding="utf-8").split("function parseApprovalInboxItem", 1)[1]


def test_backoffice_uses_authenticated_final_document_projection() -> None:
    component = (
        BACKOFFICE / "components/rfp/RfpApprovalInbox.tsx"
    ).read_text(encoding="utf-8")
    api = (BACKOFFICE / "lib/rfpApi.ts").read_text(encoding="utf-8")

    assert "getRfpFinalDocument" in component
    assert "result.coordination?.final_document_id" in component
    assert "finalDocument &&" in component
    assert "new Blob([document.markdown]" in component
    assert "URL.createObjectURL" in component
    assert "finalDocument.download_filename" in component
    assert "/approval/${encodeURIComponent(approvalRunId)}/document" in api
    assert '"/rfps/{ticket_id}/approval/{approval_run_id}/document"' in api
    assert "authFetch" in api
    assert "access_token=" not in api
    assert "token=" not in component


def test_terminated_ticket_recovers_document_through_authenticated_locator() -> None:
    page = (BACKOFFICE / "app/backoffice/rfps/page.tsx").read_text(encoding="utf-8")
    api = (BACKOFFICE / "lib/rfpApi.ts").read_text(encoding="utf-8")

    assert 'detail.status !== "terminado"' in page
    assert "getRfpFinalDocumentForTicket(detail.ticket_id)" in page
    assert "finalDocument && <FinalDocumentAccess" in page
    assert "window.sessionStorage.getItem(SELECTED_TICKET_STORAGE_KEY)" in page
    assert "window.sessionStorage.setItem(SELECTED_TICKET_STORAGE_KEY, selectedId)" in page
    assert "new Blob([document.markdown]" in page
    assert "document.download_filename" in page
    assert "/approval/document-locator" in api
    assert "const document = await getRfpFinalDocument(" in api
    assert "locator.ticket_id" in api
    assert "document.final_document_id !== locator.final_document_id" in api
    assert "access_token=" not in page
