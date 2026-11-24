# RFP Approval service

This service implements TrackFlow Hito 9, Parte 3. PostgreSQL is the business
source of truth for runs, revisions, identities, grants, decisions, commercial
inputs, conflicts, events and final documents. LangGraph checkpoints contain
orchestration state only.

## Operational flow

1. `initialize_approval_run_from_handoff()` derives the initial revisions and
   immutable validation artifacts from the persisted Parte 2 handoff.
2. Eligible imports remain `pending_assessment`. The three official conflict
   assessments must be persisted against the exact current content hashes; a
   comparable capacity assessment also requires its authorized business input.
   `assess_and_release_initial_revisions()` derives those facts only from
   persisted Parte 1 metadata, hash-verified Parte 2 artifacts and immutable
   authorized inputs. Missing capacity is `insufficient_information`, not a
   fabricated conflict.
3. `link_initial_validation_bundle()` makes a cleared revision
   `pending_approval` with a CAS update. Only then may the API start its thread.
4. Department owners inspect the safe preview/evidence and resume their own
   thread through authenticated API operations.
5. Fan-in and finalization run only after exact applied approvals and current
   proposal sources are verified.
6. `reconcile()` recovers coordination after a process interruption.

Proposal import is automatic only when the authenticated run manager opts in
at initialization with `automate_proposal_materialization=true`. The run stores
the exact manager principal, binding, general role, grant id and grant version.
After the last department approval, the completion coordinator reconstructs
that persisted delegation, revalidates `manage_approval_run` under lock and
imports the unchanged Part 2 proposal before rebuilding fan-in. Revoked,
expired, reassigned, non-manager and legacy runs remain fail-closed. Department
approvers never inherit the grant and the audit actor remains the manager who
explicitly delegated the deterministic operation.

The initializer is fail-closed for missing or divergent Part 2 artifacts.
Allowlisted commercial unknowns may instead enter Parte 3 as an explicitly
indicative, non-binding proposal with `needs_human_review`; the approver must
acknowledge the exact current pending codes. Failed compliance, unsafe preview,
unknown codes, contradictory sources and invalid provenance remain strict
blockers. Optional authorized business inputs, including
`monthly_storage_volume`, can enrich a later revision without rewriting the
facts extracted from the PDF. The service never invents prices, volume,
currency conversions, capacity or discount percentages.

The API exposes that flow without accepting an actor from the request:
`POST /rfps/{ticket}/approval/{run}/business-inputs` resolves the official
responsible and exact grant from PostgreSQL, while
`POST /rfps/{ticket}/approval/{run}/departments/{department}/revisions/{revision}/correct`
builds a fresh draft, evaluation, safe preview and validation bundle from the
selected immutable inputs. The corrected revision keeps its Parte 2 generation
counter and requires a fresh manager-driven assessment plus a new HITL thread;
no decision on the predecessor is reusable. Finally,
`POST /rfps/{ticket}/approval/{run}/assess-and-start` derives assessments,
commits them and only then opens the cleared HITL threads.

`POST /rfps/{ticket}/approval` accepts the automation opt-in only
with an effective run-management grant. Without it, the existing authenticated
manager endpoint can still materialize and reconcile the proposal explicitly.

`GET /rfps/approval/inbox` is the authenticated Backoffice read entrypoint. It
revalidates the exact `approve_department` grant for every official department
and returns only active, current `pending_approval` revisions in the caller's
environment. The review endpoint exposes only the validated safe preview,
evidence and provenance. Decisions then use the existing authenticated HITL
resume endpoint; the browser never supplies a principal or authorization id.
An admin role without an effective departmental grant receives no actionable
revision.

## Required runtime configuration

- `TRACKFLOW_ENVIRONMENT`: exactly `qa` or `production`.
- `TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH`: absolute `.sqlite`/`.sqlite3` path.
- `TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR`: allowed parent directory for checkpoints.
- `LANGGRAPH_STRICT_MSGPACK=true`.

SQLite checkpointing is suitable for the currently tested persistent MVP. A
PostgreSQL checkpointer remains an operational follow-up if multi-instance
deployment or the accepted architecture requires it literally.

## Validation boundary

The integrated academic E2E uses the official ModaViva PDF, real HTTP,
PostgreSQL, Redis, relay, Celery, LangGraph, authenticated HITL and finalization.
Only the Parte 1 model boundary is the explicit `controlled_local_stub`; Parte
2 uses the deterministic provider. That run verified automatic fan-in, one
indicative FinalDocument, replay, reconcile, hashes and idempotency with exit
code 0. It is academic QA evidence, not a production-readiness claim and not an
end-to-end accreditation of the real FourGeeks provider.

## Transaction boundary

Store and coordinator functions never commit or roll back. API/worker callers
own the transaction. Initialization atomically orders run/revision creation and
artifact/bundle persistence. Assessment, clearance and CAS bundle linking are a
separate authorized stage because they require structured facts that are not
invented by the initializer. Checkpoint thread creation follows clearance and
is recoverable by exact replay.
