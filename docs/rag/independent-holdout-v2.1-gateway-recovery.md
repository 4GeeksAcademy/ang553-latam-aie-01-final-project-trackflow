# Holdout v2.1 gateway diagnosis and checkpoint recovery

Date: 2026-11-07
System: `rag-hybrid-v3`
External requests during this diagnosis: **0**

## Demonstrated facts

The gateway client uses one shared `httpx.Client` per evaluation attempt with a
30-second scalar timeout, redirects disabled and `HTTPTransport(retries=0)`.
The embedding and generation methods use the same client. The runner closes
that client and the separate Qdrant client in `finally` blocks. A monotonic
post-request clock enforces at least two seconds between gateway calls in the
same process, including after a failed request. Exact request-start timestamps
were not persisted, so the code and offline tests prove the lower-bound
contract, not the observed interval of every historical pair.

Attempt 1 ran from `2026-11-07T22:32:57.754369Z` to
`2026-11-07T22:34:12.166928Z`. `ihv2-p03` completed embedding in 6,731.7 ms and
Qdrant in 190.9 ms, then generation ended with the sanitized `timeout`
category and no provider HTTP status. Generation had already succeeded for
`p01` in 11,485.676 ms and for `p02` in 4,029.928 ms.

Attempt 2 ran from `2026-11-07T22:48:30.755667Z` to
`2026-11-07T22:49:03.599887Z`. Its first and only gateway operation was the
`p03` embedding. It ended after approximately 30,580.7 ms with category
`timeout`, no provider HTTP status, no usage response and no Qdrant request.
The two failures therefore affect different models and stages.

The request bodies are small. The three embedding JSON bodies are 243, 261 and
231 UTF-8 bytes for `p01`, `p02` and `p03`. The complete successful generation
bodies for `p01` and `p02` are approximately 2.4 KB and 2.1 KB. Reconstructing
`p03` with only its known rank-1 chunk is also about 2.1 KB, but that is a size
estimate rather than the exact failed request because the complete trace was
not persisted. There is no local evidence of payload-size pressure.

These observations prove the local timeout boundary and failed stages. They do
not prove rate limiting, provider saturation, an upstream model timeout or an
account problem. No HTTP 429 or other provider response was received.

## Timeout recommendation

There is not enough evidence to justify a new numeric timeout. Raising the
single global value would also lengthen embedding, generation and Qdrant waits
together, masking which dependency is slow. Keep 30 seconds until 4Geeks
confirms its gateway/model latency and timeout contract. If a later authorized
instrumental change is needed, use separately configured connect/read limits
per provider operation and record them in a new attempt; do not silently alter
the frozen evaluation. Checkpoints, rather than automatic retries, are the
current recovery mechanism.

## Immutable stage checkpoints

`pipelines/eval/rag/evaluation_checkpoints.py` adds an evaluation-only layer
outside the 29-file frozen manifest. It persists successful stages atomically
and refuses overwrite:

1. `embedding_completed` contains the numeric vector needed to avoid another
   paid embedding call. It is never printed.
2. `retrieval_completed` contains candidate identifiers, scores, retrieval
   kinds and text hashes. Chunk text is reconstructed only from the frozen
   canonical corpus.
3. `generation_completed` contains the generated answer and returned usage.
4. `fidelity_adjudicated` is reserved for a separately attributable verdict.

Every checkpoint is bound to the case and question hash, holdout and rubric
hashes, evaluation binding, freeze-manifest hash, all four corpus hashes,
system version, attempt number and its predecessor checkpoint hash. A future
authorization must additionally bind the exact checkpoint file hashes it is
allowed to reuse, every prior result artifact, a new output path and the exact
remaining request budget recalculated from the verified completed stages.

The checkpoint adapter wraps the frozen `RAGService`; retrieval, prompt,
chunking, threshold and scoring code are not modified. After an embedding
timeout there is no reusable stage. After a generation timeout with a valid
retrieval checkpoint, a later authorized attempt can perform generation only.
Technical errors remain outside Recall@3 and abstention denominators.

No reusable checkpoint can be created retroactively for the two historical
`p03` attempts. Attempt 1 did not persist the complete retrieval trace, and
attempt 2 did not complete the embedding. The next controlled attempt still
requires a maximum of 14 embeddings and 14 generations for `p03` through
`n04`; successful stage checkpoints would reduce any later continuation.

## Sanitized support report

- Gateway: `https://llm.4geeks.ai/v1` (no credential included).
- Embedding model: `online/openrouter/perplexity/pplx-embed-v1-0.6b`.
- Generation model: `online/openrouter/deepseek/deepseek-v4-flash`.
- Attempt 1 window: `2026-11-07T22:32:57.754369Z` through
  `2026-11-07T22:34:12.166928Z`.
- Failure 1: generation, timeout at the 30-second client boundary, HTTP status
  unavailable. Embedding and local Qdrant retrieval had succeeded.
- Attempt 2 window: `2026-11-07T22:48:30.755667Z` through
  `2026-11-07T22:49:03.599887Z`.
- Failure 2: embedding, timeout after approximately 30,580.7 ms, HTTP status
  and usage unavailable; Qdrant and generation were not reached.
- Retries: zero. Redirects: disabled. Calls: sequential with a configured
  two-second minimum interval.
- Questions, contexts, vectors, credentials and provider bodies are omitted.

Support should confirm whether either model has a gateway-side execution or
queue timeout, expected latency percentiles, concurrency or account quotas,
and whether a request/correlation identifier can be returned before model
execution so future timeouts can be traced.

## QA

- Checkpoint/resume tests: 10 passed.
- RAG and API offline regression: 62 passed.
- Python compilation: passed.
- Ruff and Black were not available in the local virtual environment and were
  not installed.

The system is **READY FOR CONTROLLED RESUME** only after a new, explicit
authorization binds a new output path, attempt number, checkpoint directory and
maximum 14/14 request budget. This document itself authorizes no external call.
