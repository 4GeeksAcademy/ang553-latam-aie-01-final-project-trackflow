# RAG technical closure and pre-PR handoff

Date: 2026-11-07
Branch: `feature/rag-knowledge-base`
Decision: **READY FOR DRAFT PR** with incomplete academic evaluation disclosed.

The implementation, reproducibility documentation and offline QA are ready for
human review. This is not a claim that `rag-hybrid-v3` has met the current
Recall@3 requirement: holdout v2.1 stopped twice at `p03`, so its final metric
is not calculable. No Git write operation has been performed.

## Exact staging inventory

The following 18 tracked modifications belong to the delivery:

- `.env.example`
- `docker-compose.yml`
- `memory-bank/progress.md`
- `pyproject.toml`
- `services/api/main.py`
- `services/api/requirements.txt`
- `tests/test_global_serialization_contracts.py`
- `uis/backoffice/components/layout/BackofficeHeader.tsx`
- `uis/backoffice/next-env.d.ts`
- `uis/backoffice/next.config.ts`
- `uis/backoffice/package-lock.json`
- `uis/backoffice/package.json`
- `uis/Dockerfile`
- `uis/website/.gitignore`
- `uis/website/next-env.d.ts`
- `uis/website/package-lock.json`
- `uis/website/package.json`
- `uv.lock`

The following 77 currently untracked files belong to the delivery:

- `.gitattributes`
- `docs/company-knowledge-base/trackflow-carrier-coverage.en.md`
- `docs/company-knowledge-base/trackflow-returns-policy.en.md`
- `docs/company-knowledge-base/trackflow-sla-delivery.en.md`
- `docs/company-knowledge-base/trackflow-storage-pricing.en.md`
- `docs/operations/project-learnings-supabase-recovery.md`
- `docs/operations/supabase-and-local-startup-runbook.md`
- `docs/rag/academic-delivery.md`
- `docs/rag/independent-evaluator-brief.md`
- `docs/rag/independent-evaluator-package-manifest.json`
- `docs/rag/independent-holdout-v2-methodology.md`
- `docs/rag/independent-holdout-v2-validation.md`
- `docs/rag/independent-holdout-v2.1-gateway-recovery.md`
- `docs/rag/independent-holdout-v2.1-live-evaluation.md`
- `docs/rag/independent-holdout-v2.1-preflight.md`
- `docs/rag/independent-holdout-v2.1-resume-plan.md`
- `docs/rag/pre-pr-handoff.md`
- `docs/rag/project-learnings-rag-knowledge-base.md`
- `docs/rag/rag-design.md`
- `docs/rag/security-and-evaluation-handoff.md`
- `pipelines/eval/rag/__init__.py`
- `pipelines/eval/rag/api-e2e-result.json`
- `pipelines/eval/rag/calibration-results.json`
- `pipelines/eval/rag/current-system-evaluation-plan.json`
- `pipelines/eval/rag/evaluation_checkpoints.py`
- `pipelines/eval/rag/evaluation-results.json`
- `pipelines/eval/rag/fidelity-cases.json`
- `pipelines/eval/rag/fidelity.py`
- `pipelines/eval/rag/generation-cases.json`
- `pipelines/eval/rag/generation-fidelity-review.json`
- `pipelines/eval/rag/generation-results.json`
- `pipelines/eval/rag/generation-review.json`
- `pipelines/eval/rag/independent-holdout-review-findings.md`
- `pipelines/eval/rag/independent-holdout-review-protocol.md`
- `pipelines/eval/rag/independent-holdout-v1-review-template.json`
- `pipelines/eval/rag/independent-holdout-v1-rubric.json`
- `pipelines/eval/rag/independent-holdout-v1.json`
- `pipelines/eval/rag/independent-holdout-v2-rubric.json`
- `pipelines/eval/rag/independent-holdout-v2.1-binding.json`
- `pipelines/eval/rag/independent-holdout-v2.1-execution-authorization.json`
- `pipelines/eval/rag/independent-holdout-v2.1-execution-outcome.json`
- `pipelines/eval/rag/independent-holdout-v2.1-fidelity-review.json`
- `pipelines/eval/rag/independent-holdout-v2.1-p02-adjudication.json`
- `pipelines/eval/rag/independent-holdout-v2.1-results.json`
- `pipelines/eval/rag/independent-holdout-v2.1-resume-01-authorization.json`
- `pipelines/eval/rag/independent-holdout-v2.1-resume-01-results.json`
- `pipelines/eval/rag/independent-holdout-v2.1-review-amendment.json`
- `pipelines/eval/rag/independent-holdout-v2.1-review-approved.json`
- `pipelines/eval/rag/independent-holdout-v2.1-review-template.json`
- `pipelines/eval/rag/independent-holdout-v2.1-review.json`
- `pipelines/eval/rag/independent-holdout-v2.1-rubric.json`
- `pipelines/eval/rag/independent-holdout-v2.1.json`
- `pipelines/eval/rag/independent-holdout-v2.json`
- `pipelines/eval/rag/negative-results.json`
- `pipelines/eval/rag/rag-hybrid-v3-freeze-manifest.json`
- `pipelines/eval/rag/resume_independent_holdout.py`
- `pipelines/eval/rag/run_api_e2e.py`
- `pipelines/eval/rag/run_fidelity_review.py`
- `pipelines/eval/rag/run_generation.py`
- `pipelines/eval/rag/run_independent_holdout.py`
- `pipelines/eval/rag/run_retrieval.py`
- `pipelines/eval/rag/test-queries.json`
- `pipelines/eval/rag/validate_independent_holdout.py`
- `pipelines/rag.py`
- `services/api/routes/rag.py`
- `services/rag/__init__.py`
- `services/rag/chunking.py`
- `services/rag/clients.py`
- `services/rag/config.py`
- `services/rag/contracts.py`
- `services/rag/service.py`
- `tests/pipelines/test_rag_resume.py`
- `tests/pipelines/test_rag.py`
- `tests/test_rag_api.py`
- `uis/backoffice/app/backoffice/knowledge-base/page.tsx`
- `uis/backoffice/lib/ragApi.ts`
- `uis/backoffice/types/rag.ts`

The academic result and authorization JSON files above contain no credential,
vector or provider body. They are required to preserve the truthful multi-attempt
evaluation history. The incorrectly attributed earlier review is retained only
with its explicit amendment and replacement record.

## Mandatory exclusions

Do not stage:

- `.env` or any `.env.*` containing private values;
- `.tmp-tests/`;
- `.next/`, `node_modules/`, `.venv/`, `__pycache__/`, `.pytest_cache/`;
- `*.tsbuildinfo`, coverage output or build caches;
- Docker volumes or exported/expanded Compose configuration;
- local logs, provider response bodies or temporary probes;
- `independent-holdout-v2.1-checkpoints/` or any future vector checkpoint;
- any temporary diagnostic artifact not listed in the inventory above.

The private `.env` is ignored and untracked. A filename-only secret scan of the
candidate set found no real secret assignment or credentialed database URL. Its
single match is an intentional fake credential string in
`tests/test_rag_api.py` used to verify log sanitization.

## QA captured at closure

- 83 Python tests pass for RAG, checkpoints, API, JWT, roles and serialization.
- Website full ESLint and TypeScript pass.
- Backoffice RAG-focused ESLint and full TypeScript pass.
- Website and Backoffice production builds pass on Next.js 16.4.5.
- Backoffice global lint retains three errors and one warning in historical
  profile/login/AuthContext/inventory files; no RAG file is involved.
- Offline production audits report zero known vulnerabilities for both npm
  applications. Development-only findings remain separate historical evidence.
- `uv lock --check` passes. A Python vulnerability scanner is not installed.
- Compose validation passes. API, Website, login and Knowledge Base respond
  locally. Qdrant is green with exactly 14 points, 1024 dimensions and Cosine.
- `git diff --check` passes.

## Proposed Git commands

Review the inventory again immediately before running these commands. The
commands are suggestions only and were not executed during this closure.

```powershell
git add -- .gitattributes .env.example docker-compose.yml memory-bank/progress.md pyproject.toml services/api/main.py services/api/requirements.txt tests/test_global_serialization_contracts.py uis/Dockerfile uv.lock
git add -- docs/company-knowledge-base docs/operations docs/rag pipelines/eval/rag pipelines/rag.py services/api/routes/rag.py services/rag tests/pipelines/test_rag.py tests/pipelines/test_rag_resume.py tests/test_rag_api.py
git add -- uis/backoffice/app/backoffice/knowledge-base uis/backoffice/components/layout/BackofficeHeader.tsx uis/backoffice/lib/ragApi.ts uis/backoffice/types/rag.ts uis/backoffice/next-env.d.ts uis/backoffice/next.config.ts uis/backoffice/package.json uis/backoffice/package-lock.json
git add -- uis/website/.gitignore uis/website/next-env.d.ts uis/website/package.json uis/website/package-lock.json
git status --short
git diff --cached --check
git diff --cached --stat
git commit -m "feat: deliver TrackFlow RAG knowledge base"
```

## Proposed Draft PR

Title:

`feat(rag): add authenticated knowledge base with hybrid retrieval`

Description:

```markdown
## Summary

- adds four canonical TrackFlow policy documents, deterministic 14-chunk
  semantic partitioning and idempotent Qdrant indexing;
- adds the authenticated `POST /knowledge/query` FastAPI route for admin and
  manager roles, with sanitized 503 handling and controlled abstention;
- adds `rag-hybrid-v3`: semantic top-three retrieval, frozen 0.57 primary
  threshold, deterministic lexical evidence and bounded complementary context;
- adds source traceability and the protected Backoffice Knowledge Base UI;
- adds reproducible offline tests, real historical artifacts, guarded holdout
  runners and immutable stage-checkpoint tooling;
- aligns Website and Backoffice on Next.js 16.4.5.

## Verification

- 83 focused Python tests pass for RAG, API, JWT, roles, checkpoints and
  serialization;
- Website full lint/typecheck/build and Backoffice focused
  lint/typecheck/build pass;
- production npm audits report zero known vulnerabilities;
- Qdrant is green with 14 unique 1024-dimensional Cosine points and complete
  contractual payloads;
- API, Website, login and Knowledge Base respond locally.

## Academic evidence

- the earlier threshold-only retrieval version scored Recall@3 9/10 = 90%;
- that historical score does not accredit `rag-hybrid-v3`;
- holdout v2.1 contains 12 positive and four negative frozen cases and was
  approved with disclosed independence limitations;
- attempt 1 completed p01/p02 and timed out during p03 generation;
- attempt 2 timed out during p03 embedding;
- p01 fidelity passed; p02 retains its frozen fidelity FAIL;
- final v3 Recall@3 is not calculable and the current ≥80% requirement remains
  pending.

## Known limitations

- intermittent gateway timeouts remain an external operational incident under
  investigation; no provider-side root cause or rate limit is asserted;
- future evaluation requires a new explicit authorization and a new immutable
  result artifact; no automatic retry is enabled;
- Backoffice global lint retains unrelated historical findings outside RAG;
- Python dependency vulnerability scanning was not available locally;
- the evaluation is prospective with documented independence limitations, not
  strictly independent.

## Acceptance still required

- human review of the complete diff and academic disclosures;
- eventual completion of the frozen v3 holdout, if gateway stability and a new
  consumption authorization permit it;
- formal evaluator acceptance of the repository-authorized `pipelines/` path
  instead of the academic `data/` location.
```

## Next milestone

The repository roadmap identifies **Milestone 8 — Agents** after RAG. For
TrackFlow, the most coherent objective is a first-line customer-support agent
that can use the authenticated knowledge base and, later, tracking/returns
tools without duplicating policy logic.

Known dependencies and risks:

- the agent may consume RAG as a tool, but must distinguish provider errors from
  legitimate abstention;
- gateway stability and external-call budgets remain operational risks;
- JWT/role boundaries must be defined independently for staff and end users;
- agent prompts and tool contracts must not modify the frozen RAG evidence;
- shared API routes and observability must avoid exposing policy text or
  credentials.

Recommended branch after the Draft PR is accepted:

`feature/agents-customer-support`

Do not branch the next milestone from an unreviewed dirty state. First complete
human diff review and create the user-owned RAG commit/Draft PR.
