# RFP approval E2E runbook

This runbook defines the evidence required for the TrackFlow Hito 9 flow from
an official PDF to a downloadable final document. It does not authorize an
execution. PostgreSQL, Docker, workers and external model traffic require a
separate preflight and explicit approval.

## Official scenarios

| Fixture | Expected scope | Currency | Required QA evidence |
| --- | --- | --- | --- |
| `tests/fixtures/rfp_intake/trackflow-request-1.pdf` | Warehouse + Reverse | EUR | Any capacity or correction must be recorded by the responsible owner; no order/SKU conversion may be invented. |
| `tests/fixtures/rfp_intake/trackflow-request-2.pdf` | Warehouse + Last Mile | USD | Warehouse capacity and any Last Mile rate must be recorded as authorized human inputs with their own provenance. |

The PDFs are inputs, not proof of the expected result. The test must assert the
persisted classification, selected departments and currency after intake.

## Required isolated environment

1. A new PostgreSQL 16 database and container used only by this run, bound to
   loopback, with tmpfs or a new disposable volume and an exclusive network.
2. Fresh API and worker images built from the reviewed worktree.
3. A local deterministic LLM stub, or a separately authorized model budget.
4. A persistent checkpoint file in a dedicated QA runtime directory with
   `LANGGRAPH_STRICT_MSGPACK=true`.
5. Synthetic QA principals and bindings for the run manager, Ana Whitfield,
   Carlos Vega and Sofía Ramos. Their grants must be persisted and must never
   share issuer/subject values with production.
6. All database and container guardrails must name the exact verified target.
   Previous gate variables and credentials must not be reused.

The migration order is `rfp_intake.sql`, `rfp_response_generation.sql`,
`rfp_approval.sql`, `rfp_approval_extension.sql`,
`rfp_approval_monthly_storage_volume.sql`, then
`rfp_approval_indicative_validation_bundle.sql`. The new columns
`started_by_general_role` and `automate_proposal_materialization`, the eighth
authorized input kind and the indicative-bundle constraint must exist before
the API starts.

## API-only business sequence

The E2E must drive business state through HTTP and workers. Direct SQL is
permitted only for isolated identity/grant setup and read-only evidence.

1. Upload the official PDF with `POST /rfps` and wait for the intake job.
2. Assert the ticket metadata and departments with `GET /rfps/{ticket_id}`.
3. Start Parte 2 with `POST /rfps/{ticket_id}/response` and wait for a durable
   `rfp-response-v1` handoff.
4. As the QA run manager, call `POST /rfps/{ticket_id}/approval` with a fresh
   idempotency key and `automate_proposal_materialization=true`. Assert that the
   response reports `proposal_automation_delegated=true`.
5. Optionally enrich a proposal with an actually authorized missing business
   fact through `POST /rfps/{ticket_id}/approval/{run_id}/business-inputs` as
   its official QA owner. The request must not contain a principal or
   authorization id. An indicative academic proposal may instead preserve the
   allowlisted missing fact visibly and require exact human acknowledgement;
   it must not invent a value merely to achieve a complete quotation.
6. Call `POST /rfps/{ticket_id}/approval/{run_id}/assess-and-start`. Assert all
   three official outcomes and inspect any missing input code. A missing fact
   must not create an arbitration conflict.
7. For each released revision, retrieve its review projection and confirm its
   safe preview, content hash, validation hash and immutable evidence before
   submitting a decision.
8. Submit Warehouse, Last Mile and Reverse decisions independently through the
   exact department decision endpoint using Ana, Carlos and Sofía respectively.
   After the first decision, prove the other threads remain interrupted.
9. After the final applicable approval, assert that the completion node uses
   the persisted manager delegation, creates the exact Part 2 proposal,
   rebuilds fan-in, persists one final document and closes the ticket.
10. Kill and restart the API process before a final `POST .../reconcile` call.
    The call must replay without another decision, proposal, event sequence or
    document.
11. Retrieve `GET /rfps/{ticket_id}/approval/{run_id}/document` and verify the
    public Markdown projection, currency, sections, id and hash. The public
    contract intentionally omits the internal manifest; verify that manifest
    separately with read-only SQL against the approved revision ids and
    proposal id.

If a demonstrated conflict exists, the corresponding arbitration node and
authorized arbitrator must run before step 9. Information insufficiency must
return to step 5 instead of creating a conflict.

## Acceptance evidence

Capture outside the repository:

- synchronous stdout, stderr, exit code, timestamps and SHA-256 hashes;
- container id, image digest, PostgreSQL version and loopback binding;
- ticket, response run, approval run and checkpoint thread ids;
- interrupt snapshots proving independent branches;
- input, assessment, conflict, resolution, decision and event ids;
- proposal and final-document hashes and the public download;
- counts proving one current revision per department, one current proposal and
  one final document;
- zero residual test schemas and zero new connections after teardown.

Do not report the Hito complete if the E2E skips, uses inserted approvals,
reuses production identities, omits the automatic proposal step, or requires a
manual materialization call despite requesting automation.

## Executable runner

`tests/test_rfp_approval_official_e2e.py` implements the API-only sequence for
both official PDFs. It never provisions infrastructure, opens PostgreSQL or
constructs an authentication context. The API must receive already-issued QA
bearer tokens and perform its normal cryptographic/local verification.

The two network scenarios remain skipped unless all of these variables are
provided for an explicitly authorized isolated execution:

- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_AUTHORIZED=YES`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_BASE_URL=http://127.0.0.1:<port>`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_MANIFEST=<absolute external JSON path>`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_INTAKE_ADMIN_TOKEN`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_RUN_MANAGER_TOKEN`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_WAREHOUSE_TOKEN`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_LASTMILE_TOKEN`
- `TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_REVERSE_TOKEN`

The URL guardrail accepts only a credential-free loopback IP with an explicit
port. The manifest must be outside the repository, smaller than 1 MB and
declare `environment: "qa"` plus exactly `modaviva` and `luna` under
`scenarios`. Each scenario contains:

```text
{
  "carrier_model": "<one allowed carrier model>",
  "business_inputs": [
    {
      "actor": "<manager or applicable department>",
      "request": { <complete AuthorizedBusinessInputRequest fields> }
    }
  ]
}
```

The string above documents the required shape only; it is not an executable
commercial input. The external manifest must supply real, authorized QA facts,
immutable provenance, timestamps and IDs. Do not commit it or derive missing
values from orders/SKUs.

After the environment-specific preflight and authorization, run exactly:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q --tb=long -ra -p no:cacheprovider tests/test_rfp_approval_official_e2e.py
```

The runner asserts intake classification/currency/departments, a durable Part 2
handoff, delegated initialization, authorized inputs, authenticated corrected
department revisions for every `blocked_input`, all three assessments against
the corrected hashes, independent fresh interrupts, applied human approvals,
automatic FinalDocument, ticket `terminado` and an idempotent reconcile
returning the same document.
It does not inspect PostgreSQL and therefore does not verify the internal
FinalDocument manifest; that evidence remains a separate read-only SQL step.

## Integrated academic runner

`tests/test_rfp_hito9_academic_e2e.py` is the reproducible integrated Hito 9
gate. It uses the original ModaViva PDF and real isolated PostgreSQL 16, Redis,
relay, Celery worker, API, LangGraph, authentication, HITL and persistence. Its
Parte 1 model boundary is explicitly `controlled_local_stub`; Parte 2 uses the
deterministic provider. It is therefore academic integration evidence, not an
end-to-end FourGeeks accreditation and not a production-readiness test.

Install the Python project from `pyproject.toml`/`uv.lock` using the repository's
normal virtual environment, and install the Backoffice from its committed
`package-lock.json`. Build fresh backend images from
`services/task_queue/Dockerfile` and the UI image from the repository root with
`uis/Dockerfile`. Do not place `.env`, credentials or evidence in either build
context.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
npm ci --prefix uis/backoffice
docker build -f services/task_queue/Dockerfile -t <reviewed-backend-tag> .
docker build -f uis/Dockerfile -t <reviewed-ui-tag> .
```

The runner is opt-in and requires these values:

- `TRACKFLOW_HITO9_ACADEMIC_E2E_AUTHORIZED=YES`
- `TRACKFLOW_HITO9_ACADEMIC_E2E_API_IMAGE=<fresh reviewed image>`
- `TRACKFLOW_HITO9_ACADEMIC_E2E_WORKER_IMAGE=<fresh reviewed image>`
- `TRACKFLOW_HITO9_ACADEMIC_E2E_EVIDENCE_DIR=<new restricted external directory>`

The evidence directory must already exist, be empty, be absolute and remain
outside the repository. After a successful preflight, execute exactly one node:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q --tb=long -ra -p no:cacheprovider `
  'tests/test_rfp_hito9_academic_e2e.py::test_modaviva_academic_pdf_reaches_indicative_final_document'
```

The accredited run completed with exit code 0. It verified Spanish Parte 2,
independent Ana/Sofía decisions, automatic fan-in, one non-binding
FinalDocument, separate departmental/global pending codes, terminal ticket and
run states, replay HTTP 409, reconcile HTTP 200, canonical hashes and no
duplicate proposal, document or applied decision. Sanitized evidence and raw
restricted diagnostics remain outside Git.

When a separately authorized QA run uses FourGeeks, the worker-side durable
request guard also requires
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED`,
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT`,
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID`,
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID`,
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256` and
`TRACKFLOW_RFP_INTAKE_FOURGEEKS_MAX_REQUESTS`. The manifest is immutable,
job/PDF-bound and reserves each attempt before network access. This guard caps
requests, not tokens or monetary cost, and is not used by the academic stub.

## Separate real-restart evidence

The executable runner does **not** restart the API process. Its final
`reconcile` call proves same-process idempotency only. A real-restart claim
requires a separate, supervised demonstration against the same isolated QA
database and checkpoint file:

1. Drive one official scenario through the HTTP sequence above and record the
   ticket, approval run, revision, thread and final-document identifiers. Stop
   immediately after the last approval returns and before the first manual
   `reconcile` request.
2. Record the API process identifier and start timestamp, stop that API process
   gracefully, and verify that it has exited. Keep PostgreSQL and the dedicated
   checkpoint storage intact.
3. Start a new API process from the reviewed worktree with the same QA
   configuration, database and checkpoint path. Record its distinct process
   identifier and start timestamp and wait for its health check.
4. Using the already-issued QA manager token, call the existing
   `POST /rfps/{ticket_id}/approval/{run_id}/reconcile` endpoint with the
   recorded department revision. Do not submit another human decision.
5. Retrieve the existing final document and terminal ticket through HTTP, then
   use read-only SQL to compare the document id/hash and the counts of applied
   decisions, proposals, final documents and finalization events captured
   before and after the restart.

This procedure proves recovery and replay after a real API restart. It does
not, by itself, prove recovery from an arbitrarily timed process kill inside an
uncommitted transaction; that stronger claim requires a separately designed
fault-injection scenario and must not be inferred from the runner.

### Recovery claims kept separate

There are two different demonstrations and their evidence must not be mixed:

- **A — existing-document replay after restart:** complete finalization, record
  the FinalDocument id/hash, restart the API and reconcile without another
  decision. The document id/hash and all business row counts must remain
  unchanged. The procedure above demonstrates this case when it is executed.
- **B — committed approval before final coordination:** stop the API after the
  human decision/application transaction has committed but before the final
  coordination call, restart it with the same PostgreSQL and checkpoint
  storage, then reconcile. The current graph supports this path and its
  offline regression is
  `test_reconcile_recovers_after_commit_before_finalization_without_new_decision`,
  but the production HTTP call is synchronous and exposes no deterministic
  pause at that boundary. A real QA proof therefore needs a test-only ASGI
  launcher that places a parent-controlled barrier immediately before
  `coordinate_completion`, terminates that QA process after the barrier is
  reached, and restarts the normal uninstrumented API. Do not add a production
  failure flag or infer B from A.

## Authorized QA data inventory

The PDFs are source evidence only for facts they actually state. They are not
evidence for a new human quote, capacity, conversion or commercial correction.
The external manifest keeps the following shape without placeholder values:

```text
{
  "environment": "qa",
  "scenarios": {
    "modaviva": {
      "carrier_model": "<authorized allowed value>",
      "business_inputs": [
        {
          "actor": "<official responsible>",
          "request": {
            "input_id": "<new UUID>",
            "input_kind": "<contract input kind>",
            "payload": "<complete typed payload; no unknown figures>",
            "provenance": {
              "evidence_type": "<responsible_attestation|approved_quote|approved_policy|authorized_conversion>",
              "evidence_id": "<new immutable evidence UUID>",
              "source_name": "<QA source>",
              "issued_at": "<aware timestamp>",
              "valid_until": "<optional date>",
              "source_payload_hash": "<SHA-256 of the new evidence payload>",
              "notes": "<optional>"
            },
            "idempotency_key": "<new UUID>",
            "recorded_at": "<aware timestamp>"
          }
        }
      ]
    },
    "luna": { "carrier_model": "<authorized allowed value>", "business_inputs": [] }
  }
}
```

The empty Luna list above is a schema illustration, not authorization to treat
missing facts as documented. A complete commercial quotation requires the
following facts from the official responsible with new, verifiable provenance.
The academic indicative route may instead preserve the corresponding pending
codes and obtain exact human acknowledgement without supplying invented values:

| Scenario | PDF facts retained | Deterministic intake expectation | Missing authorized QA facts | Blocker while absent |
| --- | --- | --- | --- | --- |
| ModaViva | Spain/Portugal online operation, Warehouse near Zaragoza + Reverse, approximately 15,000 SKUs, ERP API, and client-managed Last Mile | EUR derived from the documented client/operation country; the PDF does not quote a currency | Sofía's Reverse processing quote for `RETURN_PROCESSING_RATE_REQUIRED`; a documented average monthly storage volume in a supported unit if the corrected proposal requires it | Visible indicative pending codes, or `blocked_input` when a complete quotation is explicitly required; no invented SKU-to-m3 conversion |
| Luna Cosmetics | Los Angeles/US operation, Warehouse + shipping across the US, approximately 5,000 orders/month, carrier selection required and requested start in about three weeks | USD derived from the documented US location; the PDF does not quote a currency | Carlos's Last Mile quote for `LAST_MILE_RATE_REQUIRED` when TrackFlow manages the carrier; Ana's capacity in the same supported unit/scope as the required volume; a documented average monthly storage volume if required | Visible indicative pending codes, `blocked_input`, or `insufficient_information` with `WAREHOUSE_CAPACITY_REQUIRED` / `CAPACITY_NORMALIZATION_REQUIRED`; no order-to-m3 conversion |

Every new quote must use `client_price_quote` and an `approved_quote` or
`approved_policy` whose payload hash covers that new quote. Capacity
must use `warehouse_capacity` with Ana's evidence. A currency conversion, if
ever needed, must use `currency_conversion` plus `authorized_conversion`; a
currency label change is not a conversion.

The departmental transition is executable through the authenticated correction
endpoint: the official responsible selects persisted input ids, the server
derives all content and hashes, and the manager subsequently runs the three
assessments before any new HITL thread is started. The typed
`monthly_storage_volume` input records value, volume unit, monthly period,
operational scope, validity, identity and evidence without rewriting the PDF.
When no such authorized fact exists, `MONTHLY_STORAGE_VOLUME_REQUIRED` remains
visible in the indicative proposal and FinalDocument. Do not encode SKUs/orders
as cubic meters or use a free-text term as a substitute.
