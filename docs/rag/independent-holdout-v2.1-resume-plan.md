# Holdout v2.1 interruption diagnosis and resume plan

Date: 2026-11-07
System: `rag-hybrid-v3`
Decision: **READY FOR RESUME AUTHORIZATION**

## Immutable parent attempt

The parent result remains unchanged at SHA-256
`10367faa8fb5835f2a2dc09fa4a65b3caf5e3d878c8b3c48e9d3a6d49fa841b0`.
It records three embedding and three generation requests, completed `ihv2-p01`
and `ihv2-p02`, then a generation timeout for `ihv2-p03`. No negative case or
multisource case ran.

The parent stores complete retrieval traces for the two completed cases. For
`ihv2-p03`, the result stores only the generation error. Sanitized runtime logs
show the expected high-demand chunk at semantic rank 1, but neither its complete
candidate trace nor the exact generated context is stored in the immutable
result. That log evidence is useful diagnostically but is insufficient for a
generation-only academic continuation.

## `ihv2-p02` adjudication

The answer is commercially correct for the direct question: it gives 5% and
the correct country-monthly-fee base. The question itself already establishes
the two-consecutive-month condition, so failure to repeat that condition is not
misleading in conversational context. The 90% threshold is only implicit in
the word “commitment” and is not stated in the answer.

The frozen rubric nevertheless requires the 90% monthly country-level SLA, the
two-month trigger and the compensation/base. Its historical 1/3 and FAIL remain
unchanged. A separate adjudication labels the answer commercially correct but
rubric-incomplete; it does not replace the frozen score.

## Timeout diagnosis

The effective provider timeout is 30 seconds. The client disables redirects and
uses `httpx.HTTPTransport(retries=0)`. `ihv2-p03` completed embedding in 6,731.7
ms and Qdrant in 190.9 ms, then generation raised the sanitized `timeout`
category with no provider HTTP status. Earlier cases in the same attempt
generated successfully in 11,485.676 ms and 4,029.928 ms. A prior documented
human generation attempt also ended near the same configured timeout.

This evidence confirms the failed operation and category, but not its external
cause. No HTTP 429 was received; rate limiting, provider saturation and account
access problems remain unproven. No timeout or retry policy is changed.

## Continuation options

1. **Resume the interrupted evaluation — selected plan.** Reuse immutable
   `p01/p02`; rerun the incomplete `p03` through the full embedding, retrieval
   and generation path; then continue through `p04`–`p12` and `n01`–`n04`.
2. **Generation-only retry for `p03` — rejected.** The exact original context
   was not persisted, so reconstructing it from logs and the current corpus
   would not be the same recorded pipeline attempt.
3. **New full 16-case run — not selected.** It would repeat successful cases
   and must be reported as a distinct evaluation, not a continuation.

The resume is attempt 2, never a claim of one uninterrupted run. It writes
`independent-holdout-v2.1-resume-01-results.json`, references the immutable
parent hash, and refuses to overwrite either artifact. Combined metrics reuse
only completed `p01/p02` and add completed resume cases. The original `p03`
timeout remains in the attempt history.

## Exact new budget

Fourteen cases remain from `p03` through `n04`:

- maximum 14 new embedding requests;
- maximum 14 new generation requests;
- maximum 28 new external requests;
- minimum two-second interval;
- zero automatic retries;
- stop and persist on the first new technical error.

Negative cases that abstain before generation may reduce actual generation
usage, but authorization must not exceed these maxima. Cumulative maxima across
both attempts would be 17 embeddings and 17 generations; actual parent usage is
preserved separately from resume usage.

## Authorization boundary

The resume runner defaults to offline validation. Live mode requires a new
attributable authorization record bound to the parent and evaluation-binding
hashes, exactly one execution and the 14/14/28 budget. No such authorization
exists yet. The previous authorization was consumed by attempt 1 and cannot be
reused.

The holdout, rubric, retrieval, prompt, corpus, threshold, models and Qdrant are
unchanged. The resume tool and its tests are outside the 29-file frozen-system
manifest and do not alter the evaluated implementation.
