# RAG security and evaluation handoff

Date: 2026-11-07

## Dependency security

The Backoffice is pinned to Next.js `16.4.5` and
`eslint-config-next` `16.4.5`. Its production dependency audit reports zero
known vulnerabilities. ESLint, TypeScript, the production build, login and
Knowledge Base routes, API rewrites, and the bounded
`experimental.proxyTimeout=90000` setting were verified after installation in
the `interfaces` container.

The same container also builds `uis/website`. That application is now aligned
on Next.js and `eslint-config-next` `16.4.5`, with React and React DOM retained
at `19.2.4`. Its production dependency audit also reports zero known
vulnerabilities. The shared interfaces image was rebuilt, both persistent
`node_modules` volumes were refreshed without deletion, and the recreated
container reports `16.4.5` for both applications.

Full audits still report four high and eight moderate findings in each
application's development-only lint/tooling tree. Neither production audit has
a residual finding, and no forced or unrelated dependency update was applied.

## Private credential rotation

Rotate both credentials personally before sharing the branch, logs, or an
export of this development session. Never paste either value into chat, source
control, shell history, or diagnostic output.

### 4Geeks gateway key

1. Create or rotate the key in the authorized 4Geeks control plane.
2. Replace only `FOUR_GEEKS_API_KEY` in the ignored local `.env` file.
3. Recreate the API process so it receives the new environment:
   `docker compose up -d --force-recreate --no-deps api`.
4. Confirm `/health` and authenticated API access locally. A gateway request is
   not required for configuration-only verification. Run one bounded gateway
   probe only under a separate explicit consumption authorization.

The key is consumed by the API RAG settings and by host-side RAG indexing or
evaluation commands that load the same environment. It is not required by the
Backoffice browser or Qdrant.

### PostgreSQL credential in `DATABASE_URL`

1. The configured provider is Supabase. In the Supabase Dashboard, open the
   project and reset the database password under **Database > Settings**.
2. Open the Dashboard **Connect** panel and preserve the existing connection
   mode, host, port, username, database, query options and SQLAlchemy driver.
   Replace only the password component, percent-encoding reserved URI
   characters.
3. Update all four ignored private variables because local inspection confirms
   that they currently share the rotated password: `DATABASE_URL`,
   `TRACKFLOW_PIPELINE_DATABASE_URL`,
   `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL`, and
   `TRACKFLOW_REPORTING_DATABASE_URL`.
4. Recreate the API. Recreate `scheduler` only if that optional profile is
   running; it consumes `DATABASE_URL` and launches the pipeline variables.
   Recreate `celery-worker` and `task-relay` only if the async profile is
   running; they consume `TRACKFLOW_REPORTING_DATABASE_URL`. Host-side pipeline
   commands pick up the new values on their next process start. Do not recreate
   Qdrant or interfaces for this rotation.
5. Verify `/health`, a legitimate login, and one existing DB-backed read. A
   failed DB-backed operation must be investigated without printing the URL.

Use `docker compose config --quiet` for structural validation. Do not use
`docker compose config` or environment-dump commands because they may expand
and print private values.

## Independent `rag-contextual-v2` evaluation

The prospective input is frozen in
`pipelines/eval/rag/independent-holdout-v1.json`; its rubric is frozen in
`independent-holdout-v1-rubric.json`. The validator enforces unique questions,
no exact reuse of historical frozen questions, all four source documents,
high-demand coverage, independent negative cases, and matching rubric IDs.

- Holdout SHA-256: `3565f8cad364745000df1c43328b30601510af9760ff718463c6de9c9a1d30a6`
- Rubric SHA-256: `860b80288e9f682762d8eb59c1bb31d5be7354fae673b76b5f7bba9ec18a0382`
- Scope: 12 positive and 4 negative questions.
- Metrics: primary Recall@3, complementary-context behavior, negative
  abstention, commercial fidelity, technical error rate, and latency.
- External maximum: 16 embedding requests and at most 16 generation requests,
  sequentially, at least two seconds apart, with zero automatic retries.

Before execution, an independent reviewer must approve the references and
rubric without changing the implementation. Recompute and compare both hashes,
then execute the frozen set once. Store regression results separately from the
independent result. Do not tune code, threshold, prompts, corpus, or grading
rules from holdout outcomes. Any later fix requires a new versioned holdout.

The preparatory review found that references and rubric facts are grounded, but
strict independence is not established: the implementation-side agent authored
and inspected the set, and six cases overlap prior evaluated intents. The
detailed findings, guarded runner, blank review record, and isolated-review
procedure live under `pipelines/eval/rag/`. Execution remains blocked pending
human adjudication, credential rotation verification, and explicit external
budget authorization.

No current-system metric is claimed by preparing these files, and no gateway
request was made during this handoff.

## Current blocking gates

1. The user must rotate the 4Geeks key and the shared Supabase password, update
   all four private database URLs, and confirm completion without posting any
   value.
2. Post-rotation checks must recreate only credential consumers and verify DB,
   login/JWT, Qdrant and frontend versions. A single 4Geeks probe still requires
   separate limited authorization.
3. A reviewer isolated from implementation must adjudicate the disclosed
   holdout overlap. The supplied review template is intentionally unapproved.
4. Only after those gates may the user authorize the maximum 16 embedding and
   16 generation requests. Until then, no real result or current metric exists.

## Superseding closure status

The gate list above records the earlier handoff and is retained for history.
Subsequent work completed credential rotation and verified both PostgreSQL
bases, API health, legitimate admin login, JWT identity, role enforcement,
Website, Backoffice and Qdrant. The v2.1 holdout also received an attributable
human approval that explicitly acknowledged its independence limitations.

Two separately authorized live attempts then stopped at `ihv2-p03`: the first
on a generation timeout and the second on an embedding timeout. Their immutable
result hashes and the original `p02` fidelity FAIL are preserved. The current
security gate is therefore clear for Draft PR preparation, while academic
accreditation remains incomplete because full `rag-hybrid-v3` Recall@3 cannot
be calculated.

The final dependency state is:

- Website and Backoffice use Next.js 16.4.5;
- production-only offline npm audits report zero known vulnerabilities in both
  applications;
- `uv.lock` is synchronized with `pyproject.toml`;
- a Python vulnerability scanner is not installed, was not installed during
  closure and remains a disclosed tooling limitation;
- `.env` is ignored and untracked; the candidate delivery set contains no real
  credential assignment or credentialed database URL. The only scanner match
  is an intentional fake secret/URL in a sanitization test fixture.

Do not stage `.tmp-tests/`, `.next/`, `node_modules/`, `*.tsbuildinfo`, vectors,
local checkpoint data, private logs or expanded Compose output. Review records
and result artifacts may be published only as the disclosed academic evidence
already represented in the pre-PR inventory; no secret value is required for
reproduction.

Any future holdout continuation requires a new explicit authorization. It must
bind all prior attempt hashes, any reusable checkpoint hashes, a new result
filename and the exact remaining request budget. Timeouts remain 30 seconds and
automatic retries remain disabled unless a separately reviewed system version
changes that contract.
