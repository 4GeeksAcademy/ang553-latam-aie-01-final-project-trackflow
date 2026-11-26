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
    assert "void executeQuestionRef.current(pending, false);" in page
    assert page.index("const pending = consumePendingSupportQuestion();") < page.index(
        "void executeQuestionRef.current(pending, false);"
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


def test_support_memory_contract_is_additive_validated_and_session_scoped():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    client = (BACKOFFICE / "lib/supportApi.ts").read_text(encoding="utf-8")
    types = (BACKOFFICE / "types/support.ts").read_text(encoding="utf-8")
    assert '`${BASE_URL}/support/memory/decision`' in client
    assert "isMemoryProposal(item.memory_proposal)" in client
    assert "UUID_PATTERN.test(item.conversation_id)" in client
    assert "item.verification_status === \"user_reported\"" in client
    assert "continue_support_question" in client
    assert "continue_support_question" in types
    assert "continue_with_user_text" not in client + types
    assert "memory_proposal?:" in types
    assert "conversation_id?:" in types
    assert "window.sessionStorage.setItem" in page
    assert "conversationStorageKey(userId)" in page
    assert "storedConversationId(userId)" in page
    assert "SUPPORT_CONVERSATION_KEY_PREFIX" in page
    assert "crypto.subtle.digest" in page
    assert "${suffix}" not in page
    assert "executeQuestionRef.current" in page
    assert "}, [userId]);" in page
    assert "const activeConversationId = conversationIdRef.current;" in page
    assert 'action: "topic_change"' not in page
    assert "caught.statusCode === 404" in page
    assert "memory_consent" in client
    assert "memory_consent?:" in types
    assert "localStorage" not in page
    assert "decisionLoading" in page
    assert 'void decideMemory("edit")' in page


def test_support_restores_conversation_and_recovers_pending_memory_after_oidc():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    client = (BACKOFFICE / "lib/supportApi.ts").read_text(encoding="utf-8")
    types = (BACKOFFICE / "types/support.ts").read_text(encoding="utf-8")
    restore = page.index("const stored = storedConversationId(userId);")
    initialize = page.index("initializeSupportAuthorizationOnce()", restore)
    assert restore < initialize
    assert "getPendingSupportMemory(recoveryIdentifier)" in page
    assert "findLegacyDecisionReference()" in page
    assert "proposalId: legacyReference.proposal_id" in page
    assert '`${BASE_URL}/support/memory/pending?${query.toString()}`' in client
    assert "SupportPendingMemoryResponse" in types
    assert "body.proposal.conversation_id !== body.conversation_id" in client
    assert "body.proposal.id !== identifier.proposalId" in client
    assert "setMemoryScope(recoveredProposal.scope)" in page
    assert 'setClientId(recoveredProposal.client_id ?? "")' in page
    assert "conversationStorageKey(userId)" in page


def test_primary_chat_memory_is_bound_to_authenticated_client_conversation():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    component = (BACKOFFICE / "app/backoffice/support/ChatMemoryConsent.tsx").read_text(encoding="utf-8")
    transport = (BACKOFFICE / "lib/useSupportChat.ts").read_text(encoding="utf-8")
    client = (BACKOFFICE / "lib/supportApi.ts").read_text(encoding="utf-8")
    card = page[page.index("<ChatMemoryConsent"):page.index("{chat.generationActive && <p")]
    assert 'chat.connection === "connected"' in page
    assert "clients.length === 1" in page
    assert "JSON.stringify([userId, clientId, chat.sessionId])" in card
    assert "!chat.generationActive" in card
    assert "pending.conversation_id !== conversationId" in component
    assert "currentChatMemoryProposal(pending.proposal, conversationId, clientId)" in component
    assert "currentAuthorizationOnly: true" in component
    assert 'cache: "no-store"' in client
    assert "memory_proposal" not in transport
    assert "Herramientas avanzadas y memoria (HTTP secundario)" in page


def test_primary_chat_memory_decisions_are_explicit_and_reload_authoritative_state():
    source = (BACKOFFICE / "app/backoffice/support/ChatMemoryConsent.tsx").read_text(encoding="utf-8")
    decide = source[source.index("const decide = async"):source.index("return (\n    <>")]
    assert decide.index("decisionRef.current = request;") < decide.index("await decisionKey")
    assert decide.index("settledRef.current !== revision", decide.index("await decisionKey")) < decide.index(
        "await decideSupportMemory"
    )
    assert "idempotency_key: idempotency.key" in decide
    assert "await reload();" in decide
    assert "setView(null);" in decide
    assert "result.proposal" not in decide
    assert "setItem(" not in source
    assert "La corrección crea otra propuesta y requerirá una nueva confirmación." in source


def test_support_decision_redirect_persists_only_user_bound_minimal_intent():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    start = page.index("interface StoredDecisionIntent")
    end = page.index("function decisionIntentStorageKey", start)
    intent_contract = page[start:end]
    assert "user_id: string" in intent_contract
    assert "conversation_id: string" in intent_contract
    assert "proposal_id: string" in intent_contract
    assert "action: SupportMemoryAction" in intent_contract
    assert "idempotency_key: string" in intent_contract
    assert "content" not in intent_contract
    assert "token" not in intent_contract.lower()
    assert "password" not in intent_contract.lower()
    assert '`${SUPPORT_DECISION_INTENT_KEY_PREFIX}:${userId}`' in page
    persist = page.index("storeDecisionIntent(intent);", page.index("const decideMemory"))
    send = page.index("await submitDecision(intent", persist)
    assert persist < send
    assert "caught instanceof SupportAuthorizationRedirect" in page


def test_support_interrupted_decision_requires_explicit_same_key_retry():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    assert "Reintentar aprobación" in page
    assert "no se ha reenviado automáticamente" in page
    assert "retryInterruptedDecision" in page
    retry = page[page.index("const retryInterruptedDecision"):page.index("const submit = async")]
    assert "interruptedIntent.idempotency_key" not in retry
    assert "submitDecision(" in retry
    submit = page[page.index("const submitDecision"):page.index("const decideMemory")]
    assert "idempotency_key: intent.idempotency_key" in submit
    assert "decideSupportMemory" in submit
    assert "crypto.randomUUID" not in submit


def test_support_change_of_user_clears_visible_state_and_stale_intent():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    start = page.index("if (!userId) {")
    unauthenticated = page[start:page.index("const stored = storedConversationId(userId);", start)]
    assert "setConversationId(null)" in unauthenticated
    assert "setProposal(null)" in unauthenticated
    assert "setInterruptedIntent(null)" in unauthenticated
    assert "setResult(null)" in unauthenticated
    assert "intent.user_id !== userId" in page
    assert "intent.proposal_id" in page
    assert "intent.conversation_id" in page


def test_support_preserves_visible_answer_during_same_user_request():
    page = (BACKOFFICE / "app/backoffice/support/page.tsx").read_text(encoding="utf-8")
    execute = page[page.index("const executeQuestion = useCallback"):page.index("useEffect(() => {")]
    assert "setResult(null)" not in execute
    assert "setResult(next)" in execute
