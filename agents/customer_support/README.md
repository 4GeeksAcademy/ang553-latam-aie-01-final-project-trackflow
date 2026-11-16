# TrackFlow customer-support graph

This package migrates the existing knowledge-base orchestration to an explicit
LangGraph without changing the frozen RAG implementation. The graph calls
`RAGService.retrieve()` exactly once and passes its returned tuple unchanged to
`RAGService.generate_answer()`.

The compiled flow is:

`START -> retrieve_context -> (abstain | generate_answer) -> (abstain | finalize_answer) -> END`

`CustomerSupportAgent.execute()` returns the existing `QueryResult` contract plus
a UUID `run_id`. It compiles with `InMemorySaver`; because checkpoints contain the
question and retrieved chunks, the thread is deleted after every terminal or
failed run. This Part 1 checkpointer does not support restart recovery or sharing
between processes.

Sanitized traces remain queryable in the process through `agent.tracer.get(run_id)`
for at most one hour and 100 completed runs by default. The same content-free
trace is logged once at completion for local Docker inspection. Traces include
only node order, transitions, timings, counts, booleans, and sanitized error
category/status. They never include questions, answers, chunks, vectors, prompts,
credentials, JWTs, or exception messages.

## Gateway resilience revision

`rag-hybrid-v3.1-resilience` preserves the graph topology and frozen retrieval
and generation semantics. Each live query has an 80-second monotonic backend
budget. Query embeddings use a process-local 256-entry, 10-minute LRU cache and
at most two selectively admitted attempts; Qdrant and generation are still
executed no more than once. Cache, retry, timeout and external-call metadata
are attached to the existing content-free node trace. The cache is not
persisted and is not shared by workers or replicas.

See `docs/rag/rag-gateway-resilience.md` for configuration, privacy limits and
the localhost acceptance runbook.

Run deterministic academic evaluations without a gateway or Qdrant:

```powershell
uv run python tests/pipelines/run_customer_support_evals.py
```

The runner uses local fakes and rewrites
`agents/customer_support/eval-results.json`. It covers an answered route,
retrieval abstention, and generation abstention. These are architecture checks,
not claims about live-model RAG accuracy.

## Deferred localhost acceptance

Run this only after the user explicitly authorizes real local acceptance. Keep
the existing ignored `.env` with the real database/JWT/gateway configuration;
do not copy private values into commands or logs. Reuse the indexed Qdrant
volume and do not run setup/reindexing.

```powershell
docker compose config
docker compose --profile rag up -d --build api interfaces qdrant
```

Open `http://localhost:3001/login`, authenticate with an existing admin or
manager account, then open
`http://localhost:3001/backoffice/knowledge-base`. Submit one known-grounded
question and one legitimately unsupported question. The response body remains
the Milestone 7 contract; obtain the graph UUID from the
`X-TrackFlow-Run-ID` response header in browser developer tools. Consult the
matching content-free completion trace with:

```powershell
$runId = "<UUID from X-TrackFlow-Run-ID>"
docker compose logs api | Select-String $runId
```

Confirm the node sequence and outcome, and confirm that the line contains no
question, answer, chunk text, vector, credential, JWT, prompt, or raw error
message. Also confirm that the existing role behavior remains unchanged: admin
and manager are allowed, ordinary users receive 403, and missing/invalid JWTs
receive 401. Stop, without deleting persistent volumes, using:

```powershell
docker compose stop interfaces api qdrant
```

Verify the controlled technical-failure route without changing credentials,
stopping providers, or consuming the gateway by running the existing fake-based
tests:

```powershell
uv run pytest tests/pipelines/test_customer_support_agent.py::test_technical_error_propagates_and_trace_is_sanitized tests/test_rag_api.py::test_provider_failure_is_sanitized
```

They verify propagation, sanitized trace metadata, checkpoint cleanup, and the
existing HTTP 503 response. Do not induce a live provider failure by damaging
private configuration. A real gateway failure check requires separate explicit
authorization from the user and ChatGPT.

## MEM-092 bounded self-evaluation

`ExternalSupportAgent` now conditionally runs `evaluate_memory` after the main
response is assembled. A deterministic prefilter admits only carrier coverage
corrections, explicitly recurrent incident patterns with at least two cases,
and monthly B2B report preferences. Ordinary questions, package-specific facts,
quoted text, memory commands and prompt-injection markers never call the model.

The optional evaluator returns a strict typed structure. When the request has a
validated memory scope and enough deadline remains, the existing configured
generation gateway can supply that structure through JSON Schema. Provider
failure, invalid JSON, timeout, exhausted budget or memory-policy rejection is
fail-closed: the primary support answer is preserved and no proposal is saved.
The graph trace records only candidate category, status and whether the
evaluator was called; it never records the question, answer, model payload or
proposed content.

## Conversational consent

If a conversation already has a pending proposal, `POST /support/query` asks
the same configured FourGeeks gateway for a strict
`trackflow_memory_consent` JSON Schema classification before entering the
normal graph. This is a separate bounded call of at most six seconds. The
classifier can approve, reject, edit, discard ambiguity, or identify a topic
change. It may also separate one support question from a clear consent response
so that the decision commits first and the remaining question traverses the
existing RAG/MCP graph once. Its category-selected JSON Schema uses only closed,
required fields; edited content is then independently revalidated by the memory
policy.

The proposal ID, identity, tenant, category and scope remain server-owned.
Edits are rebuilt from the original proposal and revalidated; they create a
replacement pending proposal and never activate memory immediately. Low
confidence becomes an ambiguous discard. Provider failure falls back only to
the conservative exact-utterance classifier; an unknown phrase becomes an
audited ambiguous discard and never leaves the proposal silently pending. A
genuine unrelated question can be submitted again after that fail-closed
transition. No substring or keyword consent matching is used. Sanitized
diagnostics distinguish timeout, provider HTTP/schema failure, invalid response
and validation failure without recording user text or proposal content.

Managers with one active client default to client scope. Managers with several
clients require an explicit authorized client. Global carrier memory requires
an explicit global scope plus an admin `AccessContext`. The evaluator can only
create a pending, `user_reported` proposal; consent and consolidation remain
separate operations owned by `services/api/agent_memory`.

## Approved-memory recovery

When the caller supplies a conversation ID, the graph runs `retrieve_memory`
after deterministic routing and before the selected operational/RAG sources.
The retrieval path is closed by query category and reads only active,
non-expired entries visible through `AccessContext`. It does not use embeddings,
Qdrant, or the frozen RAG prompt, and its output cannot change routing or tool
selection. Read failure is fail-open for the primary answer.

Relevant entries are revalidated and rendered as bounded JSON inside explicit
`user_reported`, `unverified`, `data_only` tags. Instruction-like legacy or
poisoned values are omitted. The rendered section is appended after the primary
answer as saved user-reported context; it is never represented as a verified
TrackFlow fact.

## MEM-092 acceptance status

The final technical gate is **approved with observations**. The closure suite
passed 375 automated tests: 326 memory/Support/LangGraph/RAG/MCP/frontend
contract tests, 11 tests against fresh PostgreSQL 16 in loopback `tmpfs`, and 38
OIDC tests. Real acceptance completed approval, rejection, subsequent approved
memory recovery, and B2B edit-without-auto-activation.

The strict evaluator schema was accepted by the live FourGeeks gateway and
produced the real carrier proposal. The corrected conversational-consent schema
and `ambiguous_discard` path pass deterministic regression tests, but their E2E
revalidation after the ambiguity fix remains pending because the QA Keycloak
login could not be completed. This pending check must not be reported as PASS.
