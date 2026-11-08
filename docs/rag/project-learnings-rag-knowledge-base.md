# Project Learnings — TrackFlow RAG & Knowledge Base

Status: prepared for later manual incorporation into Obsidian. This file does
not modify or depend on an external vault.

## Architecture that held up

The RAG milestone works best as a thin vertical slice rather than a standalone
application. `services/rag/` owns provider-neutral contracts, chunking,
retrieval, generation and adapters. `pipelines/rag.py` performs explicit
indexing. FastAPI reuses the service through `POST /knowledge/query`, and the
Backoffice consumes that authenticated route. This preserved the monorepo rule
that APIs live under `services/` and avoided duplicating business logic in UI or
evaluation code.

The four source documents produce 14 semantic chunks with deterministic UUIDv5
IDs. Complete commercial conditions must stay together; arbitrary token-sized
splits would separate triggers, exceptions, approvals and prices. Qdrant uses a
single `trackflow_knowledge` collection with 1024-dimensional Cosine vectors and
contractual payload fields. Deterministic IDs make duplicate detection and
idempotence observable without paying to regenerate embeddings.

## Gateway model identifiers and authentication

The identifiers initially supplied with a `litellm/` prefix did not match the
working gateway deployment. The functional identifiers are:

- `online/openrouter/perplexity/pplx-embed-v1-0.6b` for embeddings;
- `online/openrouter/deepseek/deepseek-v4-flash` for generation.

Both use the OpenAI-compatible `/v1/embeddings` and
`/v1/chat/completions` endpoints with `Authorization: Bearer`. Secrets belong
only in the ignored server-side environment. Model names, endpoint suffixes and
vector dimensions should be verified with one bounded probe before designing
downstream infrastructure; guessing them caused avoidable diagnostic work.

## Retrieval evolved from threshold-only to hybrid evidence

The calibrated semantic threshold `0.57` was useful but insufficient by itself.
One storage question retrieved a general rates section above threshold while
the answer-bearing long-term section ranked second just below it. Broad
same-document expansion fixed recall but also injected tangential holiday
policy into an SLA compensation answer. Lowering the threshold around observed
questions would have overfit the system.

The final `rag-hybrid-v3` design keeps semantic thresholding primary and adds a
deterministic lexical verification layer over the semantic top three. A lexical
candidate can establish a document only with strong corpus-discriminative
evidence; at most one further verified section from that established document
can be complementary. Complementary context cannot establish relevance by
itself. The API and UI retain primary/complementary provenance.

## Fidelity needs material-fact rubrics

Literal string checks rejected valid short answers and accepted some incomplete
ones. Commercial fidelity must instead enumerate material facts: trigger,
scope, amount, fee base, timing, exceptions and required approver. The
long-term inventory example showed that preserving day 181 and 150% is still
insufficient if the no-movement eligibility condition is omitted. The SLA
example showed the opposite risk: adding true but unrelated high-demand policy
can make an answer less precise.

Generation prompts therefore require every material qualifier while forbidding
unrequested tangential policies, unsupported formulas and false claims of
exhaustiveness. Historical results and rubrics must remain immutable even when
a later adjudication explains conversational nuance.

## Operational timeouts are not abstentions

Technical failures and information insufficiency are different outcomes. A
timeout returns a sanitized 503; it must never become the fixed abstention
answer. Instrumentation should record stage, safe category, optional provider
HTTP status, duration, request ID and question fingerprint—but never the prompt,
document text, answer, vector, credential or provider body.

The final evaluation exposed intermittent 30-second timeouts in both DeepSeek
generation and Perplexity embeddings. Successful calls before them, small
request bodies, zero retries and clean local Qdrant reads rule out several local
suspects but do not prove a provider root cause. No 429 was received. Contact
gateway support with sanitized UTC windows and model/stage details before
changing timeouts.

Long evaluations need immutable per-stage checkpoints. Persist a successful
embedding, exact retrieval trace and successful generation separately, bound to
the question, corpus, holdout, rubric, frozen implementation and prior
checkpoint hashes. This avoids paying again for completed work while keeping
every new external attempt explicit. Never manufacture a checkpoint
retroactively from incomplete logs.

## Academic evidence must remain version-specific

The historical 9/10 Recall@3 belongs to the threshold-only implementation. It
cannot accredit the later hybrid system. Holdout v2.1 was prospectively frozen
and approved with disclosed independence limitations, but two attempts stopped
at `p03`. Only `p01` and `p02` completed; `p02` retains its fidelity FAIL. Final
v3 Recall@3 is not calculable and the ≥80% requirement remains pending.

Human tests are valuable evidence of behavior—Black Friday, Aragón, long-term
inventory, international returns and abstention—but exploratory questions must
not be converted into an undeclared metric. A future evaluation must retain all
attempts, use a new authorization and output, and report itself as a
multi-attempt continuation rather than an uninterrupted or strictly independent
run.

## Delivery checklist learned

- Keep `.env`, expanded Compose output, logs, vectors and local checkpoints out
  of Git.
- Pin both frontends when they share one image; a secure Backoffice dependency
  does not protect an outdated Website dependency.
- Run production audits separately from development-tool findings.
- Verify JWT roles rather than assuming `user` means commercial staff.
- Preserve Qdrant volumes and reject incompatible collections instead of
  replacing them automatically.
- Keep offline fakes for CI and require explicit budget authorization for every
  real gateway run.
- Document partial academic status honestly; technical readiness and academic
  accreditation are separate decisions.
