"""Static frontend contracts where the repository has no browser test runner."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKOFFICE = ROOT / "uis/backoffice"


def test_support_page_is_authenticated_role_gated_and_navigable():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    header = (BACKOFFICE / "components/layout/BackofficeHeader.tsx").read_text(encoding="utf-8")
    assert "<AuthGuard>" in page
    assert 'user?.role === "admin" || user?.role === "manager"' in page
    assert 'href="/backoffice/support"' in header
    assert "user && (user.role" in header


def test_support_client_uses_authenticated_proxy_and_sanitized_telemetry_path():
    client = (BACKOFFICE / "lib/supportApi.ts").read_text(encoding="utf-8")
    config = (BACKOFFICE / "next.config.ts").read_text(encoding="utf-8")
    assert '`${BASE_URL}/support/query`' in client
    assert '{ pathTemplate: "/support/query" }' in client
    assert 'source: "/support/:path*"' in config
    assert 'destination: `${backendUrl}/support/:path*`' in config
    assert "body.detail" not in client


def test_support_ui_preserves_knowledge_base_and_honest_partial_states():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    assert "result.partial" in page
    assert "result.needs_clarification" in page
    assert "result.knowledge.sources" in page
    assert (BACKOFFICE / "app/backoffice/knowledge-base/page.tsx").is_file()
