# TrackFlow RAG — Academic delivery status

Date: 2026-11-07
Branch: `feature/rag-knowledge-base`

## Requirement matrix

| Requirement | Status | Evidence |
| --- | --- | --- |
| Four official TrackFlow documents | PASS | Canonical corpus hashes unchanged |
| Semantic chunking | PASS | 14 complete chunks, distributed 3/4/3/4 |
| Contractual payload and deterministic IDs | PASS | 14 unique Qdrant points; payload/text match |
| Qdrant collection | PASS | `trackflow_knowledge`, green, Cosine, 1024 dimensions |
| Persistence and duplicate safety | PASS | 14 IDs survived restart; no duplicate/foreign IDs |
| Real embeddings | PASS | Corpus indexing plus 16 evaluation queries |
| Calibrated abstention threshold | PASS | `RAG_MIN_SCORE=0.57`, selected only from calibration |
| Recall@3 target (threshold-only version) | PASS historical | 9/10 = 90% on the frozen final split |
| Current semantic + lexical retrieval | PARTIAL REAL / FINAL METRIC PENDING | `rag-hybrid-v3` passed offline and human Aragón/inactive-inventory checks. Holdout v2.1 stopped twice at `p03`; final Recall@3 is not calculable. |
| Black Friday retrieval | PASS | Expected high-demand section ranked first at 0.7490 |
| Negative retrieval abstention | PARTIAL | 2/3; Sunday delivery retrieved a related but insufficient chunk |
| Controlled model abstention | PASS | DeepSeek abstained for Sunday delivery despite a related retrieved chunk |
| Real grounded DeepSeek answers | CORRECTED / LIVE TEST PENDING | Historical strict result is 10/11 because `gen-storage-long-term` omits the no-movement eligibility condition. The general prompt and offline evaluator are corrected; no replacement model result has been claimed. |
| JWT and role authorization | PASS offline | Admin/manager allowed; user 403; missing/invalid JWT 401 |
| Public API contract and sanitized failures | PASS offline | Request/response/422/503 contract tests |
| Real API → Qdrant → DeepSeek E2E | PASS (integration) | JWT manager 200, user 403, missing/invalid 401; one embedding and generation. The stored answer has contradictory leading wording and is not counted as fidelity evidence. |
| Backoffice interface | PASS static | Role-aware form, loading/error/answer/sources, runtime response validation |
| Browser E2E | PARTIAL REAL | Human runs demonstrated grounded answers and abstention, but exploratory cases also recorded embedding and generation timeouts. They are not a Recall@3 dataset. |
| Frontend dependency security | PASS | Website and Backoffice run Next.js 16.4.5; both production audits report zero findings |
| Secrets and corpus integrity | PASS with historical disclosure | Credentials were rotated and post-rotation DB/login/JWT checks passed; `.env` remains ignored, the corpus is unchanged and expanded private configuration must never be included in delivery artifacts. |

The later human semantic + lexical retests supersede the functional carrier and
inactive-inventory failures as open defects: Aragón returned SEUR plus the
Carlos Vega approval rule, and inactive inventory returned the complete
commercial conditions. They do not constitute an independent holdout metric.

## Measured retrieval

The frozen evaluation input SHA-256 is
`31b3057a1af3ef6bcaaa4f142dc51997e37011c03572d7b074e7e3f6f9fa7dd0`.
The final question-level Recall@3 result is 90% (nine successful questions out
of ten) for the threshold-only version that was evaluated. The missed case asks
for reliable international coverage from Los Angeles; no point cleared the
frozen 0.57 threshold. The threshold was not changed after observing the final
split. The later contextual-retrieval change does not overwrite this evidence
and does not inherit its score without a new, separately reported evaluation.

Real evaluation embedding usage was 183 total tokens across 16 requests:

- calibration: 3 requests / 38 tokens;
- final evaluation: 10 requests / 122 tokens;
- negatives: 3 requests / 23 tokens.

## Commercial answer evidence

The intended commercial example is:

> What happens to TrackFlow delivery commitments during Black Friday,
> Christmas, and January Sales in Spain?

Retrieval passed with `sla-delivery/high-demand-periods` at rank 1. DeepSeek's
real answer was:

> TrackFlow does not guarantee delivery SLA during Black Friday, Christmas,
> and January Sales in Spain. During those dates, delivery times may extend by
> up to an additional 40%, and this must be communicated proactively to clients
> in advance.

This answer preserves every condition in the source chunk without adding a new
commercial term.

## Remaining acceptance work

The frozen eleven-case generation rubric and the twelfth authenticated API E2E
are complete. The persisted manual artifact remains an immutable record of its
original 11/11 adjudication. This final audit accepts ten cases without
qualification, but requires human adjudication of `gen-storage-long-term`: it
preserves day 181 and 150% over the standard rate while omitting that the fee
applies after more than 180 days with no movement (no sales or outbound). That
eligibility condition is commercially material and cannot be treated as merely
redundant wording.

A browser-driven live model query was not repeated because it would exceed the
authorized twelve-generation limit. Targeted UI lint, TypeScript and the
production build passed; a human reviewer should perform the final
visual/browser acceptance without changing retrieval labels, questions,
threshold or chunking.

The authenticated E2E proves the HTTP/JWT/Qdrant/model path, but its persisted
answer begins with `Yes` before saying that TrackFlow does not guarantee the
SLA. That wording is internally inconsistent, so this result is deliberately
excluded from the fidelity total. The frozen review artifact is retained
unchanged; the stricter pre-PR conclusion is ten unqualified fidelity passes
and one case pending human adjudication.

Proposed PR title after human review: `feat: rag knowledge base`.

## Final functional correction

The pre-PR functional review confirmed that the historical generation runner
injected the complete canonical chunk directly for every case. For
`gen-storage-long-term`, the reference sent to the model included the complete
condition: more than 180 days with no movement (no sales or outbound), 150%
over the standard rate, starting on day 181. Retrieval and context preparation
therefore did not cause the omission.

The generation system prompt now requires every material qualifier needed to
avoid broadening a commercial policy, including eligibility/trigger,
inactivity or movement, scope, exceptions, approvals, periods and figures. It
also requires an unambiguous, internally consistent opening for yes/no answers.

The frozen generation artifacts remain unchanged. A separate offline review is
stored in `generation-fidelity-review.json` and is bound by SHA-256 to both the
historical output and its new material-requirement profiles. It accepts the
three genuine literal-evaluator false negatives and rejects
`gen-storage-long-term` solely for the missing no-movement eligibility
condition (3/4 pass).

The official Docker environment was also exercised. Qdrant remained healthy
with 14 points, Cosine distance and 1024-dimensional vectors; FastAPI returned
200 from `/health`; and the backoffice login and Knowledge Base URLs returned
200. The frontend authentication rewrite returned the expected 401 without a
token. No model request was made. Browser automation could not reach local
ports, so visual and interactive response acceptance remained a live human test
at that stage.

## Post-human-test correction status

The human browser test confirmed the Black Friday response and exposed two
separate operational outcomes. The Sunday request reached FastAPI and completed
with HTTP 200 after 36.628 seconds, but the Next.js development proxy had
already closed its upstream connection at its 30-second default. A bounded
90-second proxy timeout is now configured and passed a 35.261-second offline
proxy reproduction.

The subsequent instrumented inactive-inventory run established the retrieval
failure precisely. `standard-rates-and-grace-period` was primary at
`0.6174929`; the answer-bearing `long-term-inventory` section ranked second at
`0.55981266` and was excluded by the primary threshold, so generation correctly
abstained from incomplete context.

The service now permits a below-threshold semantic top-k candidate only as
complementary context when a primary candidate establishes the same source
document. It does not lower `RAG_MIN_SCORE=0.57`, use a score margin, expand an
entire document, or let complementary context establish relevance. The API and
UI expose whether each source is primary or complementary. Offline regression
covers inactive inventory, Sunday delivery, out-of-corpus questions, Black
Friday and approval/exception cases; no gateway request or Qdrant mutation was
made.

The historical 90% result remains frozen evidence for the earlier
threshold-only version. The changed context methodology requires an
independent, versioned reevaluation before a metric can be claimed for the
current system. Reusing the already inspected final split is acceptable only as
a post-change diagnostic, not as fresh academic accreditation.

The first real generation attempt after contextual expansion failed technically
after retrieval succeeded. Its `40134.7` ms total is strongly consistent with
the effective 30-second gateway timeout plus the measured retrieval and bounded
inter-request interval, but the historical log did not preserve a category or
provider status. It is therefore not claimed as a confirmed timeout, HTTP 429,
or provider saturation event. Sanitized generation substage logging now makes
the next single authorized run conclusive without recording business text or
provider bodies. Offline tests cover success, timeout, HTTP 429/500, network
failure, invalid response, privacy, API 503, and authentication. No replacement
real result has been claimed.

## Final commercial-fidelity review

The canonical SLA document explicitly states that, when a country remains
below the 90% monthly on-time SLA for two consecutive months, the client is
entitled to 5% compensation on **that country's monthly fee**, under the
standard contract. The human statement using that exact economic base is
therefore supported; no additional formula is inferred.

High-demand dates are a separate policy section. Mentioning Black Friday,
Christmas, or January Sales in a compensation-only answer is factually grounded
but unnecessary and potentially distracting unless the question asks about
those periods. Generation instructions now prioritize the concrete question,
permit complementary facts only when materially needed, prohibit unsupported
economic inference, and avoid categorical claims that no other exceptions
exist when the supplied references are not explicitly exhaustive.

The complementary retrieval contract is now bounded to the single
highest-ranked below-threshold candidate per document that already has primary
evidence. The complement still must be in the semantic top-k and cannot create
document relevance. This preserves the inactive-inventory recovery path while
preventing both weak SLA fragments from being added to a compensation query.
The remaining best complement can still be tangential, so the prompt-level
materiality rule and generation abstention remain necessary safeguards.

`current-system-evaluation-plan.json` versions this policy as
`rag-contextual-v2`. It records offline checks and explicitly marks current
Recall@3, negative abstention rate, real fidelity, and latency as unavailable
without new external calls. The historical 90% is not relabeled as a current
holdout result.

The carrier question failed before Qdrant with a sanitized
`embedding_error/errorCategory=timeout` near the effective 30-second gateway
timeout. This confirms the operation and category, but not why the provider did
not respond in time; rate limiting and saturation remain unproven. No timeout,
model, retry, or consumption policy was changed. The Backoffice now explains a
503 as temporary provider unavailability while keeping it distinct from a
successful information-insufficiency abstention.

One historical PostgreSQL telemetry persistence failure remains recorded
separately. No direct dependency on RAG retrieval or generation was found, so
that subsystem was not modified.

## Final security gate and prospective evaluation

- Backoffice Next.js is pinned to `16.4.5`; its production audit reports zero
  vulnerabilities and its focused lint, typecheck, build, routes, rewrites,
  login and Knowledge Base checks pass.
- `uis/website` is aligned on Next.js `16.4.5`; its production audit moved from
  one critical, four high and one moderate finding to zero. The shared
  interfaces image and persistent dependency volumes were refreshed without
  touching API or Qdrant.
- Private 4Geeks and PostgreSQL credentials were not changed. The user-owned
  rotation procedure and exact service-recreation scope are documented in
  `security-and-evaluation-handoff.md`.
- A prospective independent set is frozen at 12 positive and four negative
  questions. It covers all four documents and high-demand periods, uses a
  separate material-fact rubric, and is hash-bound before execution.
- No independent result, current Recall@3, abstention rate, fidelity score, or
  latency metric is claimed. Execution requires independent rubric approval
  and a separate authorization for at most 16 embeddings plus 16 generations.

## Precision correction after final human checks

The final human checks demonstrated both tangential same-document expansion
and a false retrieval abstention. The retrieval layer now verifies
below-threshold semantic top-three candidates with deterministic local lexical
evidence. The semantic threshold remains `0.57`; at most one lexically verified
candidate can establish a document when no semantic primary exists, and at
most one further verified fragment from that document can be complementary.

Offline regression confirms that the SLA compensation context excludes the
holiday section, while the Aragón carrier query includes Spain coverage and
the approval exception but excludes United States coverage. Inactive inventory
still retains its material long-term conditions; Black Friday, international
returns, Sunday delivery, and out-of-corpus abstention contracts remain
covered. No gateway call, corpus edit, vector update, or Qdrant mutation was
used for this correction.

Because this is a retrieval-method change, neither the historical 90% nor the
sealed holdout automatically accredits it. The frozen files and hashes remain
unchanged and the plan now registers `rag-hybrid-v3`. The guarded runner can
record raw Recall@3, primary/complementary recovery, abstention, false positives,
latency, usage and answers. Execution remains blocked because the implementer
authored and inspected the set and six intents overlap earlier evaluation; an
isolated reviewer or human adjudication is required before any metric is
reported.

## Final offline closure checkpoint

- 91 focused RAG, FastAPI, JWT, role and serialization tests pass.
- Website and Backoffice run Next.js `16.4.5`; both production dependency
  audits report zero findings. Previously completed builds and TypeScript checks
  pass; Website full lint and Backoffice milestone-focused lint pass.
- Qdrant remains green with 14 unique 1024-dimensional Cosine points; IDs and
  complete payloads match the canonical corpus.
- The independent runner is prepared but no result file exists. It defaults to
  offline validation and requires a hash-bound independent approval plus an
  explicit 32-request acknowledgement for real execution.
- Credential rotation and holdout independence adjudication remain acceptance
  gates. No staging, commit, push, PR, corpus mutation or gateway request was
  performed during this checkpoint.

## Definitive technical closure status

This section supersedes only the obsolete operational statements in earlier
checkpoints; it does not rewrite any historical metric or result artifact.

The approved holdout v2.1 contains 12 positive and four negative cases. Its
questions, rubric, review limitations and hashes remain frozen. The first live
attempt completed `p01` and `p02`, then stopped at a DeepSeek generation timeout
for `p03`. The authorized continuation stopped at `p03` during its embedding
request. Across both attempts the evaluation consumed four embeddings and three
generations. `p01` passed retrieval and fidelity. `p02` passed retrieval but
retains its frozen fidelity FAIL because its answer supplied only one of the
three required rubric facts. The first `p03` retrieval observation found the
expected section, but the case never completed. `p04` through `p12` and all four
negative cases remain unexecuted.

Consequently, **final Recall@3 for `rag-hybrid-v3` is not calculable**. The
partial two-case retrieval observation must not be presented as academic
accreditation, and the required current-version Recall@3 of at least 80% has not
yet been demonstrated. The historical 9/10 = 90% remains valid only for the
earlier threshold-only implementation.

Human functional evidence remains useful but is not a replacement metric:

- grounded behavior was observed for Black Friday, international delivery,
  international returns, storage discounts, long-term inventory, Aragón
  carrier selection and approval exceptions, SLA compensation, and questions
  genuinely outside the corpus;
- the final exploratory round recorded an embedding timeout for Express
  California, a generated answer for a return after 25 days with only partial
  visual review, a correct DHL/exception answer, and an embedding timeout for
  inventory aged 190 days;
- the Christmas and refund-timing exploratory questions were not executed.

No percentage is derived from those exploratory observations.

The two holdout interruptions and two later human embedding timeouts are an
open external operational incident. They prove the stage and the local
30-second timeout boundary, not whether the cause is the 4Geeks gateway,
OpenRouter/upstream execution or the network path. There was no HTTP 429 or
other provider status. Calls remained sequential with a two-second minimum
interval and zero retries.

The continuation runner now supports immutable, hash-bound checkpoints for
completed embedding, retrieval, generation and later fidelity adjudication.
No historical checkpoint can be manufactured retroactively for `p03`. A future
attempt must use a new authorization and output artifact, bind all previous
attempt and checkpoint hashes, and keep the historical FAIL and timeouts in the
consolidated report.

Final closure QA on 2026-11-07 recorded:

- 83 offline Python tests passing across RAG, checkpoints, API, JWT, roles and
  serialization contracts;
- Website full lint and TypeScript passing; Backoffice RAG-focused lint and
  full TypeScript passing;
- production builds passing for Website and Backoffice on Next.js 16.4.5;
- production-only npm audits reporting zero known vulnerabilities for both
  applications using the local audit cache;
- Compose structural validation passing; API, Website, login and Knowledge
  Base responding locally; Qdrant green with 14 points, Cosine and 1024;
- Backoffice global lint still failing only in historical non-RAG files with
  three errors and one warning; this is disclosed separately and was not
  modified during RAG closure.

No gateway request, holdout execution, Qdrant rebuild, staging, commit, push or
PR occurred during technical closure.
