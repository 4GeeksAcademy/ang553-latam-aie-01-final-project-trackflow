# RAG gateway resilience — `rag-hybrid-v3.1-resilience`

## Scope

This operational revision preserves `rag-hybrid-v3`: the four-document corpus,
prompt, embedding and generation models, semantic threshold, hybrid retrieval,
sources and abstention contract are unchanged. It adds availability controls
below the existing LangGraph `retrieve_context` node. Historical evaluation
results and freeze manifests are not rewritten and do not claim to hash this
revision.

## Time budget

The local Next.js rewrite allows 90 seconds. FastAPI assigns each RAG execution
an 80-second monotonic budget, leaving 10 seconds for proxy, scheduling and
response overhead outside the measured backend work.

| Setting | Default | Rationale |
| --- | ---: | --- |
| `RAG_TOTAL_BUDGET_SECONDS` | 80 s | 10 s below the local proxy |
| `RAG_EMBEDDING_ATTEMPT_TIMEOUT_SECONDS` | 25 s | above the observed 16.68 s success; below the observed 30.79 s timeout |
| `RAG_EMBEDDING_MIN_RETRY_TIMEOUT_SECONDS` | 22 s | preserves more than 18 s of read time after phase reserves |
| `RAG_QDRANT_TIMEOUT_SECONDS` | 5 s | observed retrieval was 0.253 s |
| `RAG_GENERATION_TIMEOUT_SECONDS` | 20 s | observed generation was 3.12 s; bounded to preserve the total deadline |
| `RAG_RESPONSE_RESERVE_SECONDS` | 3 s | serialization and HTTP completion reserve |

Before every external operation the assigned timeout is the smaller of its cap
and the budget remaining after downstream reserves. Work is rejected with a
controlled `deadline_exhausted` error when a useful window does not remain.
Deadline exhaustion is HTTP 503, never a knowledge abstention.

## Embedding retry policy

There are at most two total embedding attempts. Timeouts, network errors, 429
and 500/502/503/504 are retryable. HTTP 400/401/403/404/422, configuration,
validation and malformed responses are permanent. The default backoff is
0.5 seconds with up to 0.25 seconds of jitter and a 2-second backoff cap.
A valid `Retry-After` is honored only up to the configured 5-second cap and
only when the remaining deadline admits the second attempt. Existing gateway
pacing is included in the operation timeout. The HTTP transport keeps zero
implicit retries.

Qdrant runs once after a successful embedding. Generation runs once. LangGraph
does not retry or duplicate nodes.

## Query-embedding cache

The cache is process-local, memory-only, thread-safe LRU with single-flight.
Defaults are 256 entries and a 600-second TTL; capacity zero disables it. The
derived SHA-256 key includes the trim-only normalized query, embedding model,
expected dimension, resilience version and TrackFlow language scope. The raw
query and derived key are never logged. Only finite, correctly sized vectors
from successful calls are cached. Errors, answers and invalid vectors are not.

The cache is empty after restart and independent in every worker or replica.
It provides no distributed coherence guarantee.

## Sanitized observability

One UUID is reused as HTTP run header, LangGraph thread ID and trace ID. Node
traces contain cache result (`hit`, `miss`, `coalesced`, `disabled` or failed
miss), attempt number, bounded durations/timeouts, retry decision, backoff,
remaining budget, category/status and external-call count. Final outcomes
distinguish `answered`, `abstained`, `technical_error`, `retry_exhausted` and
`deadline_exhausted`.

Traces never contain questions, prompts, vectors, chunks, model answers, JWTs,
credentials, provider bodies or exception messages. Authorized browser clients
can read `X-TrackFlow-Run-ID` through CORS.

## Localhost acceptance runbook

Validate and start without deleting volumes or reindexing Qdrant:

```powershell
docker compose config --quiet
docker compose --profile rag up -d --build api interfaces qdrant
docker compose ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/health
Invoke-WebRequest -UseBasicParsing http://localhost:3001/login
Invoke-WebRequest -UseBasicParsing http://localhost:6333/healthz
```

Run the deterministic offline resilience coverage without consuming the
gateway:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/pipelines/test_rag_resilience.py -q
```

Open `http://localhost:3001/login`, authenticate with an existing admin or
manager account, then visit
`http://localhost:3001/backoffice/knowledge-base`.

Manual cases:

1. Ask `Is the delivery SLA guaranteed during Black Friday in Spain?` only
   after a separate authorization permits a real gateway call. Confirm the
   high-demand source and a 200 response.
2. Repeat the exact question. Confirm `cache=hit` in the trace; latency alone
   is not acceptance evidence.
3. Ask a genuinely unsupported question. Confirm a 200 abstention, not 503.
4. Use `tests/pipelines/test_rag_resilience.py` for deterministic simulated
   timeout-then-success and persistent-failure cases. No provider failure is
   induced locally.
5. Confirm an ordinary user receives 403 and missing/invalid JWT receives 401.

Use the returned run ID to find the content-free trace:

```powershell
$runId = "<X-TrackFlow-Run-ID>"
docker compose logs api | Select-String $runId
```

### Accepted localhost evidence — 2026-11-08

The manual acceptance used sanitized traces only. The repeated Black Friday
requests shared question fingerprint `34151fcc97eb`, returned HTTP 200 and had
different run IDs:

| Run | `run_id` | Cache | Embedding calls | Qdrant calls | Generation calls | HTTP duration | Trace duration |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| First | `598082eb-9be1-4fcd-8027-2f52f01b4cbb` | `miss` | 1 | 1 | 1 | 13,628.7 ms | 13,331.553 ms |
| Repeat | `c58afad0-bd18-4ce4-a186-cbd23d549824` | `hit` | 0 | 1 | 1 | 15,129.3 ms | 15,069.797 ms |

Both runs selected the same two source identifiers:
`sla-delivery/high-demand-periods` and
`sla-delivery/monthly-sla-and-compensation`. Both completed as `answered` with
no retry, technical error or exhausted deadline. The hit is demonstrated by
the cache metadata, empty embedding-attempt list and zero external embedding
calls; its embedding stage completed from the process-local cache in
approximately 3.4 ms. The repeat was slower overall because its single
generation took longer. Cache acceptance therefore does not imply that total
response latency must decrease.

The unsupported live-animal transport case returned HTTP 200 under run
`b4e0fb20-1206-4075-a7bb-db3d098b1e0e`, completed as `abstained` with
`insufficient_context=true`, made no generation call and recorded no technical
error. Retry and deadline behavior were accepted through deterministic offline
tests rather than by inducing provider failures. The inspected traces contained
no raw question, answer, prompt, vector, credential, JWT or provider body.
These are operational acceptance records transcribed from the explicitly
accepted local run and retained runtime logs. Raw traces are intentionally not
committed, and these records are not hash-bound academic evaluation artifacts.

Stop services without removing volumes:

```powershell
docker compose stop interfaces api qdrant
```

## Known limitations

- Real same-process cache reuse is accepted for the evidence above. It remains
  process-local and is lost on restart; multi-worker or distributed reuse is
  outside this revision.
- The 80/25/20/5/3-second defaults are conservative initial operational
  hypotheses, not production SLOs; production proxy limits and latency
  percentiles still require measurement.
- Synchronous `httpx` cannot cancel an in-flight socket operation more exactly
  than its assigned phase timeout. Its read timeout measures inactivity between
  chunks, so an adversarial slow-trickle response could exceed the nominal
  80-second aggregate budget. The budget is an admission and per-phase control,
  not a hard thread-interruption guarantee.
- Local development runs Uvicorn and Next.js in reload/dev mode; production
  deployment behavior must be verified independently.
- Four historical byte-hash tests fail in this Windows checkout because Git
  materializes three frozen JSON inputs as CRLF although their committed blobs
  use LF. The frozen inputs, hashes and manifests must not be edited to hide the
  mismatch; line-ending policy and checkout normalization require separate
  follow-up.
- One pre-existing serialization test contains a literal Windows path and
  therefore fails in the supported Linux container. This is a test-portability
  issue, not a RAG runtime failure.
- The host Python 3.13 ASGI suite stalls on its first request. The supported
  Python 3.12 container passes the complete RAG API suite; Python 3.13 root-cause
  analysis remains separate follow-up work.
- The historical `rag-hybrid-v3` freeze manifest does not bind this operational
  v3.1 revision. Its runner is therefore expected to reject the changed runtime
  files; this revision must not rewrite the frozen manifest to make that runner
  accept it.
