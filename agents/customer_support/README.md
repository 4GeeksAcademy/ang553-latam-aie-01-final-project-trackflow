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
