# TrackFlow RAG and Knowledge Base

## Implemented scope: Block A

Block A provides a provider-neutral RAG core, deterministic corpus chunking,
HTTP adapter contracts, an authenticated FastAPI endpoint, an internal
backoffice page, an opt-in Qdrant service definition, and tests that run without
Qdrant, gateway credentials, or model consumption.

Block A did not perform real provider calls. During Block B mentoring on
2026-11-06, the embeddings endpoint and all 14 canonical chunks were verified
at 1024 dimensions. B2-R2 then indexed and verified the real Qdrant collection,
and the finalization phase measured retrieval. Real DeepSeek generation and a
JWT-authenticated API-to-model E2E were then verified. A browser-driven live
model run was not repeated because the authorized generation budget was fully
consumed; the UI is supported by targeted lint, TypeScript and production build.

## Architecture

- `services/rag/` owns provider-neutral contracts, orchestration, configuration,
  semantic chunking, and HTTP adapters.
- `pipelines/rag.py` is the explicit indexing entry point and reuses the shared
  RAG service; it does not duplicate retrieval or generation logic.
- `services/api/routes/rag.py` exposes `POST /knowledge/query`.
- `pipelines/eval/rag/test-queries.json` keeps calibration, final evaluation,
  and negative cases separate.
- `uis/backoffice/app/backoffice/knowledge-base/` provides the protected UI.

The academic `data/` route is intentionally not used. This follows the
repository-authorized `pipelines/` exception and is not evidence of evaluator
approval.

## Corpus and chunking

The original Markdown documents under `docs/company-knowledge-base/` remain
unchanged. The loader requires all four files and fails if an approved semantic
boundary is missing or ambiguous.

| Source identifier | Sections | Chunks |
| --- | --- | ---: |
| `sla-delivery` | delivery commitments; monthly SLA and compensation; high-demand periods | 3 |
| `returns-policy` | standard process; window and costs; international returns; service scope | 4 |
| `carrier-coverage` | United States; Spain; selection and exceptions | 3 |
| `storage-pricing` | rates and grace period; long-term inventory; aging reports; preferential rates | 4 |

Every Qdrant payload contains exactly the required business fields:

```json
{
  "company": "trackflow",
  "language": "en",
  "source_document": "sla-delivery",
  "section": "high-demand-periods",
  "chunk_index": 2,
  "text": "..."
}
```

Point IDs are deterministic UUIDv5 values derived from source, section, index,
and a SHA-256 digest of the text. An unchanged corpus therefore produces the
same IDs. After a successful upsert, setup removes stale TrackFlow/English
points for the four contractual source identifiers. An incompatible existing
collection is rejected and is never recreated or deleted automatically.

## Retrieval and generation

The service separates `setup`, `embed`, `retrieve`, `generate_answer`, and
`query`. Embedding, generation, and vector storage are protocols that can be
replaced by fakes.

Retrieval always filters `company=trackflow` and `language=en`, honors `k` and
an optional minimum score, and returns fewer than `k` results when appropriate.
The calibrated minimum score continues to define **primary** retrieval. A
below-threshold candidate may be added as **complementary** context only when
it is already present in the same semantic top-k result and its
`source_document` matches a document established by at least one primary
candidate. Only the highest-ranked below-threshold candidate per established
document is retained. A complementary candidate cannot establish relevance by itself,
and the service never expands to every section in a document. This bounded
rule preserves the primary threshold while allowing closely related commercial
conditions from the same policy document to reach generation without a second
embedding or Qdrant request.

Primary and complementary references are identified separately in the public
source contract and generation context. Retrieved text is wrapped as untrusted
JSON reference data. The generation system instruction forbids treating it as
instructions or inventing figures, deadlines, rates, approvals, or contractual
terms.

Following the final functional review, the instruction also requires every
material qualifier needed to avoid broadening a commercial policy: eligibility
and trigger conditions, scope, inactivity or movement requirements,
exceptions, approvals, periods and figures. Yes/no answers must start with an
unambiguous answer consistent with the explanation that follows. It also
prioritizes the concrete question, treats complementary references as usable
only when materially necessary, rejects unsupported fee bases or formulas, and
does not claim that no other conditions or exceptions exist unless the supplied
references explicitly establish exhaustiveness.

If retrieval returns no qualifying context, `query` returns a fixed
insufficient-information answer and does not invoke generation.

`RAG_MIN_SCORE=0.57` was frozen before opening the final evaluation split. It is
the two-decimal downward rounding of the lowest relevant calibration score
(`0.578724`), derived only from the three positive calibration questions. This
retained all three expected calibration sections at rank 1. Negative questions
were evaluated only after selection and were not used to adjust the threshold.

## HTTP and authorization

`POST /knowledge/query` requires the existing JWT Bearer authentication:

- `admin` and `manager`: allowed;
- `user`: `403`;
- missing or invalid JWT: `401`.

The public response contains `answer`, `insufficient_context`, and unique source
document/section references. The two traceability fields extend the academic
minimum `answer` contract because abstention and source traceability cannot be
represented safely by `answer` alone. Raw chunks, vectors, prompts, similarity
scores, and credentials are never returned.

Provider/configuration failures return a generic `503`. Upstream response
bodies and credential values are not included in public errors.

The existing roles do not prove that all eight commercial team members are
authorized. Their role assignments remain a business acceptance item.

## Gateway contract

The verified Block B endpoints are:

- `https://llm.4geeks.ai/v1/embeddings` (verified ✅, returned 1024-d vectors)
- `https://llm.4geeks.ai/v1/chat/completions`

Models (gateway-listed on 2026-11-06):

- embeddings: `online/openrouter/perplexity/pplx-embed-v1-0.6b`
- generation: `online/openrouter/deepseek/deepseek-v4-flash`

The adapters use `httpx`, Bearer authentication, distinct model identifiers,
bounded timeouts, and sanitized errors. Block A tests request shapes with
`httpx.MockTransport`; Block B verified the real embeddings endpoint, response
shape, 1024-element vectors, and real DeepSeek chat completions.

All live gateway adapters use zero automatic retries, disabled redirects,
bounded timeouts, and a shared minimum two-second pause after every request.
Embedding and generation request counts, status codes, and numeric usage are
recorded without prompts, vectors, credentials, or upstream error bodies.

## Qdrant configuration

Compose defines `qdrant/qdrant:v1.20.0` under the opt-in `rag` profile. The
service uses the existing `trackflow-dev` network, persists data in
`qdrant-data`, and exposes REST only on `127.0.0.1:6333` for development. The
canonical corpus is mounted read-only into the API container so an explicit
`python -m pipelines.rag` operator invocation can load it.

The collection is exactly `trackflow_knowledge` with cosine distance. Vector
size is derived from the first real embedding and consistency is checked across
the batch before collection setup. Real CLI indexing also requires
`RAG_VECTOR_SIZE` and rejects a returned vector of any other size before it
contacts collection setup. The verified gateway output is **1024** — confirmed
on 2026-11-06 by embedding all 14 corpus chunks.

B2-R2 indexed the canonical corpus into the local opt-in Qdrant service on
2026-11-06. The sequential run made exactly 14 embedding requests with a
minimum two-second pause, stored 14 points, and reported 891 prompt/total tokens
and zero completion tokens. Qdrant reported a green collection with Cosine
distance and 1024-dimensional vectors. A full scroll matched every deterministic
ID, payload and source text, found no duplicate or foreign points, and confirmed
the 3/4/3/4 document distribution. The same 14 IDs remained after restarting
the container, confirming named-volume persistence. Idempotence evidence is
based on deterministic-ID and duplicate checks; embeddings were not regenerated
solely to repeat the upsert.

## Evaluation contract

The evaluation artifact contains:

- 3 threshold-calibration questions;
- 10 final positive questions covering all four documents;
- 3 negative abstention questions.

The input was frozen before evaluation with SHA-256
`31b3057a1af3ef6bcaaa4f142dc51997e37011c03572d7b074e7e3f6f9fa7dd0`.
The metric is question-level Recall@3 (equivalent to hit rate here because every
case has exactly one expected source/section). The untouched ten-question final
split scored **9/10 = 90%**, above the 80% target. The only miss was
`eval-carrier-international-la`: its expected United States carrier chunk fell
below the frozen threshold and no result qualified. Black Friday passed with
the expected high-demand section at rank 1 and score `0.7490`.

Three real negative retrieval checks followed calibration. Hazardous materials
and warehouse vehicle questions abstained. Sunday delivery retrieved the
high-demand section at `0.596487`, so retrieval-only abstention scored 2/3. The
generation prompt now requires the exact `INSUFFICIENT_CONTEXT` sentinel when
retrieved evidence does not answer a question; the service maps that sentinel
to a fixed public abstention with no sources. DeepSeek correctly produced the
controlled insufficient-information answer for Sunday delivery, so the combined
retrieval-plus-generation behavior abstained successfully.

Reproducible sanitized evidence is stored in:

- `calibration-results.json`: 3/3, three embedding requests, 38 total tokens;
- `evaluation-results.json`: 9/10, ten embedding requests, 122 total tokens;
- `negative-results.json`: 2/3 retrieval-only abstentions, three embedding
  requests, 23 total tokens.

The retrieval runner is invoked as `python -m pipelines.eval.rag.run_retrieval`
with one of the frozen splits and, after calibration, `--threshold 0.57`.
`generation-cases.json` freezes eleven factual/abstention rubrics before model
output. `python -m pipelines.eval.rag.run_generation` persisted eleven real
answers. The original manual artifact records 11/11 and the automatic
literal-pattern rubric passed 7/11. The final pre-PR audit accepts ten answers
without qualification but leaves `gen-storage-long-term` for human
adjudication: its answer preserves day 181 and 150% over the standard rate but
omits the commercially material requirement that the inventory have more than
180 days with no movement (no sales or outbound). The historical result file
is not rewritten. Usage was 2,465 prompt, 1,018 completion and 3,483 total
tokens, with provider-reported cost `0.00064068956`.

`run_fidelity_review` is a separate offline, deterministic post-hoc check. Its
profiles allow equivalent wording per material fact but require every fact in
the profile. The resulting `generation-fidelity-review.json` is hash-bound to
the untouched historical result. It passes the DHL, Carlos Vega and Miguel
Torres answers and rejects the historical long-term-storage answer because it
omits the no-movement eligibility condition. It does not call the gateway or
replace the frozen 7/11 automatic and 11/11 historical manual records.

The final API E2E used isolated in-memory users with real signed JWTs, the real
Qdrant collection, one real query embedding, and one real DeepSeek generation.
It returned 200 for `manager`, 403 for `user`, and 401 for both missing and
invalid tokens. The Black Friday answer cited only
`sla-delivery/high-demand-periods`. Its persisted text begins with `Yes` before
stating that TrackFlow does not guarantee the SLA; this contradictory leading
word means the run is evidence of end-to-end integration, not an additional
fidelity pass. E2E generation usage was 193 prompt, 15 completion and 208 total
tokens; provider-reported cost was `0.00011835`.

## Block B gates

1. [x] Authorize a minimal authenticated gateway probe without logging secrets.
2. [x] Confirm `/v1`, request formats, model access, and a 1024-element embedding.
3. [x] Start the opt-in Qdrant profile and verify health/persistence.
4. [x] Index once and verify deterministic IDs/counts, no stale/foreign points,
   no duplicates, and persistence after restart without regenerating embeddings.
5. [x] Calibrate `RAG_MIN_SCORE` using only the calibration split.
6. [x] Measure Recall@3 on the untouched ten-question evaluation split: 90%.
7. [x] Complete real negative generation validation; retrieval abstained for 2/3
   and DeepSeek correctly abstained for the related Sunday chunk.
8. [x] Review real answers for exact figures, deadlines, currencies, exceptions,
   and Miguel Torres/Carlos Vega approval requirements.

## Post-human-test operational diagnosis

The first human browser run produced three FastAPI 200 responses in submission
order: 18.684 s, 8.919 s and 36.628 s. For the last request, Next.js emitted
`ECONNRESET` at approximately 30 seconds while FastAPI continued and completed
successfully 6.4 seconds later. Next.js 16.2.7 applies a 30-second proxy timeout
when `experimental.proxyTimeout` is unset. The backoffice now configures a
bounded 90-second proxy timeout; an offline test using the installed Next proxy
returned HTTP 200 after 35.261 seconds.

The exact human wording about additional fees for inactive inventory was not
part of the frozen retrieval inputs, and its query embedding, candidates and
scores were not persisted. Therefore no score or sent context can be recovered
for that execution. The canonical long-term-inventory point remains present in
Qdrant, and the related frozen evaluation wording retrieved it at rank 1 with
score `0.64591557`, but that is not evidence for the human wording.

Retrieval still applies the frozen `0.57` threshold locally to primary results.
It requests the top three Qdrant candidates without a server-side cutoff and
logs only a question fingerprint, source/section, score, retrieval kind,
qualified counts and outcome. It never logs the question, chunk text, prompt,
answer, vector or credential.

A later human run supplied the missing evidence: the storage-pricing
`standard-rates-and-grace-period` section was primary at `0.6174929`, while the
answer-bearing `long-term-inventory` section ranked second at `0.55981266` and
was discarded. The model therefore returned a controlled generation
abstention. The contextual-expansion rule above fixes the general failure mode;
it was not selected from a numeric margin around this score and does not admit
unrelated cross-document candidates.

The historical **9/10 = 90%** Recall@3 result remains evidence for the
previously evaluated threshold-only retrieval version. Because contextual
expansion changes the set of references supplied to generation, the current
version has not inherited that score automatically. It requires a separately
reported reevaluation of retrieval, negative abstention and generation
fidelity. A rerun of the already inspected frozen split may be used only as a
post-change regression diagnostic; academic accreditation should use an
independently prepared, versioned holdout to avoid post-hoc bias.

## Generation failure observability

An instrumented human query on long-term inventory completed retrieval with
the expected storage sections, then returned HTTP 503 after `40134.7` ms. The
historical event retained only `stage=generation`; it did not retain the
sanitized exception category or provider status, so its exact cause cannot be
proven retroactively.

The effective gateway timeout was 30 seconds and the shared minimum interval
was 2 seconds. The observed total is strongly consistent with the measured
8.099-second embedding, 0.185-second Qdrant lookup, the remaining inter-request
pause, and a generation request ending near the 30-second timeout. This timing
does not exclude a late HTTP response or network failure, so it is evidence for
a timeout hypothesis rather than a confirmed provider diagnosis. Rate limiting
also remains unproven because no HTTP 429 status was recorded.

Generation now emits sanitized `generation_start`, `generation_success`, and
`generation_error` events. Errors include only request ID, query fingerprint,
duration, category (`timeout`, `http_error`, `network_error`,
`validation_error`, or `provider_error`) and provider HTTP status when one
exists. The final technical-error event repeats the safe category and status.
Questions, reference text, prompts, responses, exception bodies, vectors, and
credentials are never logged. Empty or structurally invalid successful
responses are classified as validation errors. The 30-second timeout, disabled
redirects, zero HTTP retries, model, corpus, retrieval policy, and HTTP 503
behavior remain unchanged.

## Current-version evaluation and final operational evidence

`pipelines/eval/rag/current-system-evaluation-plan.json` defines the current
method as `rag-hybrid-v3` without altering any frozen input or historical
result. Offline regression can prove selection invariants, context integrity,
authentication, failure handling, and compatibility with the stored historical
artifacts. It cannot produce a new current-version Recall@3, negative
abstention rate, real-model fidelity score, or latency distribution without
fresh embeddings/generation. A future acceptance result therefore requires an
independently authored, versioned holdout; the already inspected questions may
only be reported as regression cases.

The final human batch passed international deliveries, international returns,
the 60-cubic-meter discount, and pharmaceutical out-of-corpus abstention. A
carrier-coverage query failed during embedding with a sanitized timeout near
30 seconds and never reached Qdrant. This proves the failing operation and
category, not gateway rate limiting or saturation. Timeout, model and retry
settings remain unchanged; the UI maps technical 503 responses to temporary
provider unavailability instead of presenting them as insufficient knowledge.

The canonical compensation wording was rechecked directly. The 5%
compensation base is explicitly "that country's monthly fee" after two
consecutive months below the 90% country-level SLA. High-demand conditions are
separate and need not be added to a compensation-only answer.

Dependency review on 2026-11-07 found a direct critical npm advisory affecting
the installed Next.js 16.2.7; npm identifies 16.4.5 as the fixed available
release. The repository rules require explicit approval before changing
`package.json`, so this update is an acceptance blocker rather than a silent
dependency mutation. Python's lock is consistent, but `pip-audit` is not
installed and was not installed during this review.

An earlier diagnostic expanded private Compose variables into session output.
No secret is tracked in `.env`, but the affected gateway and database
credentials should be rotated before the branch is shared. A separate
historical PostgreSQL telemetry persistence failure has no demonstrated RAG
dependency and remains outside this change.

## Technical closure and reproducibility

The final implemented method is `rag-hybrid-v3`. Semantic similarity remains
the primary signal with `RAG_MIN_SCORE=0.57`. A below-threshold top-three
candidate can become primary only through deterministic, corpus-aware lexical
evidence; at most one additional verified section from the established
document can be complementary. Primary and complementary sources remain
separate in the API and UI. Neither lexical evidence nor complementary context
changes the Qdrant corpus, vector schema or canonical 14-point collection.

The v2.1 prospective evaluation was run but did not complete. Attempt 1
completed `p01` and `p02`, then `p03` timed out during generation. Attempt 2
restarted `p03` and timed out during embedding. The immutable artifacts record
four embedding calls, three generation calls and no completed case beyond
`p02`. `p01` passed fidelity; the frozen `p02` fidelity FAIL remains. Final
current-version Recall@3 is unavailable, and the historical 90% must not be
relabeled as a `rag-hybrid-v3` result.

The gateway client uses the verified OpenAI-compatible `/v1/embeddings` and
`/v1/chat/completions` endpoints with models
`online/openrouter/perplexity/pplx-embed-v1-0.6b` and
`online/openrouter/deepseek/deepseek-v4-flash`. Bearer authentication is
server-side. A shared `httpx.Client` applies a 30-second timeout, disabled
redirects, zero HTTP retries and a minimum two-second post-request interval.
The two holdout failures and two later human embedding timeouts are logged as
an external operational incident; no provider, upstream or network root cause
is claimed without a returned status or provider trace.

`evaluation_checkpoints.py` and the continuation runner are evaluation-only
instrumentation outside the 29-file frozen implementation manifest. Successful
embedding, retrieval and generation stages can be persisted atomically and
reused only when question, holdout, rubric, binding, corpus, freeze manifest,
prior attempt and checkpoint hashes all match. Vector checkpoints must never be
printed or staged. Existing result artifacts are immutable and each future
attempt must use a new output path and explicit budget authorization.

### Local verification without gateway consumption

1. Start the normal API and interfaces services and opt in to Qdrant with the
   `rag` profile; do not use `docker compose down -v`.
2. Validate Compose structurally with `docker compose config --quiet`.
3. Check `GET http://127.0.0.1:8000/health`, Website on port 3000, Backoffice on
   port 3001 and Qdrant `healthz` on port 6333.
4. Confirm `trackflow_knowledge` is green with 14 points, 1024 dimensions and
   Cosine distance. Read-only verification must not scroll vectors into logs.
5. Run the focused offline tests for `test_rag.py`, `test_rag_resume.py`,
   `test_rag_api.py` and `test_global_serialization_contracts.py`.
6. Run frontend lint/typecheck/build using installed dependencies. Backoffice
   global lint has separately disclosed historical findings; milestone-focused
   lint must remain clean.
7. Use `npm audit --omit=dev --offline` for the reproducible local security
   check, and record when the advisory cache was used.

Indexing is not part of routine verification because it would regenerate 14
paid embeddings. Run `python -m pipelines.rag` only under a separate explicit
authorization and only when collection repair or corpus reindexing is actually
required.

## Security and independent-evaluation handoff

The Backoffice dependency is now pinned to Next.js `16.4.5`; its production
audit is clean and the installed container version passed focused lint,
TypeScript, build, rewrites, login, Knowledge Base, and the preserved
90-second proxy-timeout checks. The separate `uis/website` application in the
same interfaces image is now aligned on `16.4.5`; both production audits are
clean and the effective container reports the expected versions. Website
lint, TypeScript and build pass. Backoffice focused lint, TypeScript and build
pass; its global lint still reports pre-existing findings in unrelated profile,
login, inventory and auth files.

The holdout is prospectively frozen as 12 positive and four negative cases,
with a separate fact rubric and SHA-256 bindings recorded in
`current-system-evaluation-plan.json`. Its original artifact name retains the
`rag-contextual-v2` label, while the registered target system is now
`rag-hybrid-v3`. It has not been executed and carries no new metric. Review
found grounded references but insufficient independence: the implementation
side authored and inspected it, and six intents overlap earlier evaluation.
Human adjudication, verified credential rotation, and explicit approval for up
to 16 embeddings and 16 generations are required before execution.

## Post-human precision diagnosis: local candidate verification

Two human checks exposed opposite weaknesses in the same contextual rule. An
SLA compensation query retrieved the correct compensation section at
`0.6930667`, but the previous same-document expansion also supplied the
high-demand section at `0.37799862`; generation then repeated unrelated holiday
policy despite the materiality instruction. A carrier query returned the two
answer-bearing sections in semantic positions one and two (`0.5341613` and
`0.48770076`), but neither cleared `RAG_MIN_SCORE=0.57`, so the service
abstained before generation.

The minimal correction keeps Qdrant as semantic candidate generation and
performs a deterministic lexical evidence check only over its top three
candidates. A below-threshold candidate requires at least two distinct content
token overlaps with the question, including one token occurring in no more
than two of the fourteen canonical chunks. Accents and simple English plurals
are normalized; stop words and `TrackFlow` are ignored. This is an evidence
rule, not a replacement similarity score or a margin fitted to the observed
scores.

Semantic candidates at or above `0.57` remain primary. When a document has no
semantic primary, its highest-ranked lexically verified candidate may establish
that document as a lexical primary. At most one additional verified candidate
from the established document remains complementary. A single generic overlap
cannot establish or expand relevance. Logs identify `semantic`, `lexical`, or
`none` as the evidence basis without recording query text.

Alternatives rejected for this correction were: lowering the semantic
threshold (post-hoc calibration and wider false-positive risk); unrestricted
same-document expansion (the demonstrated contamination source); full-corpus
BM25 or a new external reranker (larger operational and latency change than the
evidence requires); and re-chunking/re-indexing (would alter the frozen corpus
representation and require new embeddings). The selected verification adds no
network request and leaves documents, vectors, Qdrant, `k=3`, and the threshold
unchanged. It cannot rescue a relevant fragment absent from the semantic top
three, which remains a deliberate residual limitation.

The repeated carrier fingerprint was not an internal retry. Sanitized logs
contain distinct request IDs at `16:28:23Z` and `16:28:43Z`, each with its own
embedding and Qdrant stages. They prove two independent API executions but do
not distinguish intentional resubmission from a browser double submit.

This changes the retrieval methodology after `rag-contextual-v2` was defined.
The sealed holdout and its hashes remain untouched and were not executed to
choose the rule. The plan registers `rag-hybrid-v3`, a guarded runner, and a
blank independent-review record. Because the implementation side had already
seen the questions, an isolated evaluator must adjudicate whether the disclosed
overlap is academically acceptable or commission a new versioned set. The
historical 90% remains evidence only for its original threshold-only system.
