# Project Learnings note — Supabase rotation and local recovery

## Situation

TrackFlow uses one Supabase PostgreSQL project with an operational database
(`postgres`) and an analytical database (`trackflow_reporting`). A project-role
password rotation was followed by authentication failures, inconsistent local
URLs, a malformed JWT assignment and disagreement between host and Docker API
behavior.

## Previous decisions

- Use one managed PostgreSQL cluster so operational and analytical databases
  share the cluster-wide role while retaining database-level separation.
- Use Session Pooler on port `5432` because the original Codespaces environment
  could not reliably reach the direct IPv6 endpoint.
- Keep operational data in `postgres` and reporting/publication state in
  `trackflow_reporting`.
- Verify separation from live database identity and OID, not URL text.
- Keep scheduler and async workers opt-in because they can produce real effects.

## Problems found

- `DATABASE_URL` contained a literal `PROJECT_ID` instead of the Supabase project
  reference.
- The four PostgreSQL URLs did not consistently use the same role password.
- `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL` was absent.
- Pipeline/reporting URLs did not reliably select `trackflow_reporting`.
- `JWT_SECRET_KEY` contained malformed quoting and later a multiline
  representation that Docker Compose could not parse consistently.
- The API container retained environment values from before `.env` was edited.
- A host Uvicorn process occupied `127.0.0.1:8000` while Docker served the
  current API on IPv6, producing different OpenAPI schemas for the same port.

## Diagnosis

Static inspection compared only sanitized URL properties: driver, pooler mode,
port, database class, project-reference equality and password equality. Live
SQLAlchemy checks then proved that `postgres` and `trackflow_reporting` were on
the same server but had different database OIDs.

The JWT value was recovered without output from the active API environment and
normalized to one escaped `.env` assignment. The port conflict was isolated by
comparing OpenAPI through `localhost`, `127.0.0.1` and `::1`, then confirming the
IPv4 process command was an obsolete TrackFlow Uvicorn instance.

## Solution

- Preserve the valid operational URL for `postgres`.
- Derive pipeline source from the operational connection.
- Derive both analytical URLs from the same pooler identity and role password,
  changing only the database to `trackflow_reporting`.
- Keep SSL required and percent-encode the password exactly once.
- Normalize JWT quoting without changing or displaying the active value.
- Validate Compose with `docker compose --profile rag config --quiet`.
- Recreate API after environment changes; use restart only when the stored
  container environment should remain unchanged.
- Stop the confirmed obsolete host Uvicorn process so Docker owns port 8000.

## Tests and evidence

- Both databases passed `SELECT 1`, `current_database()` and identity/OID reads.
- SQLModel read existing SKU, StockEntry and StockExit records.
- Telemetry insert/visibility/rollback was proven inside one isolated
  transaction, with no persistent row remaining.
- Reporting read two historical `blocked_quality` runs; no valid weekly
  publication exists.
- Authenticated API reads returned 200 for identity, inventory, telemetry and
  latest reporting run.
- Admin reached RAG request validation without invoking the gateway; an existing
  `user` role received 403.
- After controlled restart, API, Website, Backoffice and Qdrant recovered.
- Qdrant retained 14 unique points, 1024-dimension Cosine configuration and
  payloads exactly equal to the current deterministic chunks.
- Offline QA: 486 passed, two skipped, one unrelated CORS assertion failed; one
  historical Python 3.13 collection defect was reported separately.

## Lessons learned

1. A Supabase project display name is not its pooler project reference.
2. PostgreSQL role passwords are cluster-wide; separate databases do not imply
   separate passwords for the same role.
3. `restart` preserves a container's environment. Use controlled recreation to
   load `.env` changes.
4. Validate `.env` with both the application parser and `docker compose config
   --quiet`; quoting accepted by one parser may fail in another.
5. Never diagnose a port only through `localhost`; compare IPv4 and IPv6 and
   identify the owning process before stopping it.
6. Keep optional effectful profiles stopped until their schemas and execution
   are explicitly authorized.
7. Record real provisioning gaps. Configuration and mocks do not prove that
   optional async tables or published reports exist.
8. Preserve a versioned corpus hash manifest if future acceptance requires
   byte-for-byte proof; deterministic Qdrant equality proves current operational
   consistency but not historical Git identity for untracked files.

This note is prepared for manual incorporation into `09 - PROJECT LEARNINGS`.
It does not modify any external Obsidian vault.
