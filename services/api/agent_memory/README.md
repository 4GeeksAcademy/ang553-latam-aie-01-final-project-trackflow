# MEM-092 structured agent memory

This package owns the persistent, consent-gated memory used by TrackFlow's
existing customer-support agent. It is not a RAG store and never writes to
Qdrant. Callers provide an authenticated `AccessContext` and a SQLModel
`Session`; the package has no FastAPI dependency and performs no provisioning
at import time.

Provision PostgreSQL explicitly with `infra/sql/agent_memory.sql`. Proposals
store structured, policy-validated content plus an origin interaction ID and
SHA-256 digest, never a raw conversation transcript. Decisions are append-only.
An active entry always references the approval decision that authorized it.

Global memories are admin-only. Client-scoped memories require access to the
active client, and B2B reporting preferences are always client-scoped.
Pending proposals expire after 24 hours by default. Expiry records an auditable
decision before closing the proposal.

## Consent contract

`ConsentCoordinator` remains a transport-neutral boundary. The additive
`POST /support/query` contract now accepts optional `conversation_id`,
`client_id`, and `memory_scope`, and may return a safe `memory_proposal` plus
bounded memory metadata without breaking legacy clients. The server creates a
content-free, identity-owned conversation binding when the UUID is omitted.

Authenticated admin/manager clients submit decisions through
`POST /support/memory/decision`. The endpoint reuses OIDC and `AccessContext`,
recovers the proposal by identity plus `conversation_id`, and maps sanitized
not-found, conflict, policy, and persistence failures to 404/409/422/503.
Trusted clients should send the explicit `ConsentAction` enum:
`approve`, `reject`, `edit`, `ambiguous`, or `topic_change`. Requests should also
carry `proposal_id` and a stable idempotency key so an HTTP retry can return the
original decision after the proposal is no longer pending.

When `POST /support/query` receives the next user turn for a conversation with
a pending proposal, the existing Support agent classifies that turn first. The
configured FourGeeks generation gateway returns a strict JSON Schema result
bound server-side to the recovered proposal. Clear approval or rejection is
resolved before any optional support question; an edit derives a replacement
from the original scope/category and requires another confirmation; an
unrelated question discards the proposal and then traverses the normal Support
graph. A clear consent response may also carry one separately classified
support question, which is processed once after the atomic decision.

The Backoffice does not pre-classify a typed turn or convert it to
`topic_change`; it sends the next message through `/support/query`. The explicit
Accept/Reject/Edit controls remain available through `/support/memory/decision`.

An edit closes the original proposal, creates a replacement pending proposal,
and never creates a memory entry until the replacement is separately approved.
Ambiguity, silence and topic changes are audited as `ambiguous_discard`; a topic
change returns `continue_support_question=true` so the caller can process the
new question normally. Expired proposals never activate memory.

The production model-backed classifier implements the structured
`ConsentClassifier` protocol and returns an action tied to the exact proposal
ID. Low-confidence results are downgraded to ambiguity. If structured
classification is unavailable, the textual fallback
accepts only a small set of complete, exact confirmation or rejection
utterances. It never uses substring matching and never infers edits or topic
changes. An unknown fallback phrase is resolved fail-closed as an audited
`ambiguous_discard`; it never leaves the proposal silently pending. If that
phrase was actually an unrelated support question, the user can submit it again
after the pending proposal has been closed. Memory policy validation runs again
on both edits and approvals.

## Cleanup and capacity

`MemoryStore.expire_and_clean` is an explicit transactional maintenance call.
It records an append-only `expired` decision before closing each stale pending
proposal, marks time-expired entries as expired, and then enforces a default
maximum of 100 active entries independently for each scope/client/category
bucket. Capacity overflow expires the oldest active rows; it never deletes an
entry, decision, proposal, authorization link, or supersession history. The
operation uses row locks plus conditional updates and is idempotent. Category
TTLs remain 30 days for carrier coverage, 14 days for recurrent incidents and
180 days for B2B report preferences; proposal TTL remains 24 hours.

The supported operational invoker is:

```powershell
uv run --python 3.12 python -m scripts.agent_memory_cleanup --execute
```

It requires the existing PostgreSQL `DATABASE_URL`, performs no provisioning,
and emits only content-free counts. It is suitable for an operator run or for
the deployment's existing maintenance facility; no additional scheduler is
installed by MEM-092.

For periodic operation, inject `DATABASE_URL` through the deployment's existing
secret mechanism and run the command from the repository root. An hourly cadence
keeps the operational delay after the 24-hour proposal TTL to approximately one
hour; a daily cadence must be an explicit retention tradeoff. Preserve the exit
code, alert on nonzero results, avoid deliberately overlapping executions, and
never add the connection URL to the command line or logs. A second successful
run may legitimately report zero counts because cleanup is idempotent.

Technical closure passed 375 automated tests, including 11 against fresh
PostgreSQL 16. Real acceptance completed approval, rejection, later approved
memory recovery, and edit-without-auto-activation. The live FourGeeks gateway
accepted the strict memory-evaluator schema and produced the real carrier
proposal. The corrected consent-classifier schema and ambiguous discard are
covered by automated regression, while their post-fix E2E repetition through
Keycloak remains pending.

The complete architecture, API, provisioning, operations, limitations, and QA
guide is in [`docs/agent-memory.md`](../../../docs/agent-memory.md).
