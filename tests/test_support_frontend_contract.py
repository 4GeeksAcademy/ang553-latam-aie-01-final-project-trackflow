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
    assert 'path_template: "/support/query"' in client
    assert "getSupportAccessToken" in client
    assert "clearSupportAccessToken" in client
    assert 'Authorization: `Bearer ${token}`' in client
    assert "authFetch" not in client
    assert 'source: "/support/:path*"' in config
    assert 'destination: `${backendUrl}/support/:path*`' in config
    assert "body.detail" not in client


def test_support_oidc_uses_pkce_and_never_persists_access_tokens():
    oidc = (BACKOFFICE / "lib/supportOidc.ts").read_text(encoding="utf-8")
    assert 'code_challenge_method", "S256"' in oidc
    assert 'const SUPPORT_SCOPES = "openid incidents:read inventory:read"' in oidc
    assert 'return `${window.location.origin}/backoffice/support`' in oidc
    assert "sessionStorage.setItem(PKCE_VERIFIER_KEY" in oidc
    assert "sessionStorage.setItem(PKCE_STATE_KEY" in oidc
    assert "clearPkce();" in oidc
    assert "localStorage" not in oidc
    assert "sessionStorage.setItem(\"access" not in oidc
    assert "accessToken = payload.access_token" in oidc
    assert oidc.index("clearPkce();") < oidc.index("if (providerError")
    client = (BACKOFFICE / "lib/supportApi.ts").read_text(encoding="utf-8")
    assert "if (response.status === 401) clearSupportAccessToken();" in client


def test_keycloak_qa_registers_exact_backoffice_callbacks_and_origins():
    keycloak = (ROOT / "mcps/trackflow/qa/configure_keycloak.py").read_text(
        encoding="utf-8"
    )
    assert '"http://localhost:3001/backoffice/support"' in keycloak
    assert '"http://127.0.0.1:3001/backoffice/support"' in keycloak
    assert '"http://localhost:3001"' in keycloak
    assert '"http://127.0.0.1:3001"' in keycloak


def test_compose_passes_only_public_support_oidc_configuration():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT" in compose
    assert "NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT" in compose
    assert "NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID" in compose
    assert "NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_SECRET" not in compose


def test_api_image_installs_the_pinned_mcp_adapter_runtime():
    requirements = (ROOT / "services/api/requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "mcp>=1.28,<2" in requirements
    assert "langchain-mcp-adapters==0.3.2" in requirements


def test_qa_seed_hashes_runtime_local_password_for_dual_session_acceptance():
    seed = (ROOT / "mcps/trackflow/qa/seed_trackflow.py").read_text(
        encoding="utf-8"
    )
    assert 'required("QA_TRACKFLOW_LOCAL_PASSWORD")' in seed
    assert "hashed_password=hash_password(local_password)" in seed
    assert 'hashed_password="external-oidc-only"' not in seed


def test_support_ui_preserves_knowledge_base_and_honest_partial_states():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    assert "result.partial" in page
    assert "result.needs_clarification" in page
    assert "result.knowledge.sources" in page
    assert (BACKOFFICE / "app/backoffice/knowledge-base/page.tsx").is_file()


def test_support_ui_replays_one_pending_question_after_pkce_callback():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    assert 'PENDING_SUPPORT_QUESTION_KEY = "trackflow_support_pending_question"' in page
    assert "window.sessionStorage.setItem(PENDING_SUPPORT_QUESTION_KEY, normalized)" in page
    assert "caught instanceof SupportAuthorizationRedirect" in page
    assert "if (!retainAcrossRedirect) clearPendingSupportQuestion();" in page
    assert "initializeSupportAuthorizationOnce()" in page
    assert "const pending = consumePendingSupportQuestion();" in page
    assert "void executeQuestion(pending, false);" in page
    assert page.index("const pending = consumePendingSupportQuestion();") < page.index(
        "void executeQuestion(pending, false);"
    )
    assert "clearPendingSupportQuestion();\n        setError(" in page
    assert "localStorage" not in page


def test_support_pending_question_is_consumed_before_callback_replay():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    consume = page.index("function consumePendingSupportQuestion")
    clear = page.index("clearPendingSupportQuestion();", consume)
    normalize = page.index("pending?.trim()", consume)
    assert clear < normalize
    assert "supportAuthorizationInitialization ??=" in page
    assert "normalized.length <= 2000" in page
