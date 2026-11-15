# TrackFlow MCP QA SQL Provisioning Fix

**Status:** ✅ FIXED
**Severity:** Critical (launcher blocking)
**Date Fixed:** 2024-11-14

## Problem Statement

The QA launcher (`start.sh`) failed after successfully starting Keycloak:

```
psycopg2.errors.UndefinedTable: relation "trackflow_client" does not exist
```

This occurred at `seed_trackflow.py` execution, line 53 of the incidents models insert.

## Root Cause Analysis

### Architecture Discovery

1. **TrackFlow has TWO separate table management systems:**
   - `SQLModel.metadata`: Used by services.api (inventory, auth)
   - `operational_security_metadata`: Used by services.incidents (client, membership, incident, history)

2. **The incident tables are EXPLICITLY PROVISIONED:**
   - Defined in `services/incidents/models.py` on separate SQLAlchemy metadata
   - Deliberately do NOT participate in `create_db_and_tables()`
   - Production provisioning: `infra/sql/incident_query_security.sql`
   - QA provisioning: **MISSING IN LAUNCHER**

3. **The launcher sequence was incomplete:**
   ```
   ✓ Start PostgreSQL
   ✓ Configure Keycloak
   ✓ Harden Keycloak
   ✗ Apply incident DDL (MISSING)
   ✗ Seed data (fails because tables don't exist)
   ```

## Solution Implementation

### Files Modified

**1. `mcps/trackflow/qa/codespaces/start.sh`**
- **Change:** Insert DDL provisioning step between hardening and seeding
- **Line:** Added after line 105 (after `harden_keycloak.py`)
- **Implementation:**
  ```bash
  uv run --python 3.12 python mcps/trackflow/qa/codespaces/provision_qa_ddl.py
  uv run --python 3.12 python mcps/trackflow/qa/seed_trackflow.py
  ```
- **Rationale:** Ensures tables exist before seed attempts INSERT

### Files Created

**2. `mcps/trackflow/qa/codespaces/provision_qa_ddl.py` (124 lines)**
- **Purpose:** Apply explicit DDL to QA database
- **Function:**
  1. Validates `TRACKFLOW_MCP_QA=1` environment marker
  2. Validates `DATABASE_URL` points to loopback QA instance (127.0.0.1:55440)
  3. Reads `infra/sql/incident_query_security.sql` from repository root
  4. Executes DDL via `psycopg2` with `ON_ERROR_STOP` semantics
  5. Verifies all 4 operational tables were created:
     - `trackflow_client`
     - `user_client_membership`
     - `incident`
     - `incident_history`
  6. Reports success or propagates SQL errors for debugging

- **Security:**
  - Rejects non-QA connections (port check)
  - Only creates tables in ephemeral QA database
  - Uses environment-provided connection credentials (no hardcoding)
  - Validates database actually exists before operations

**3. `mcps/trackflow/qa/codespaces/test_qa_provisioning.py` (251 lines)**
- **Regression Tests:** 3 comprehensive test cases

  1. **`test_ddl_provisioning_creates_four_tables`**
     - Verifies DDL provisioning creates exactly the 4 required tables
     - Uses ephemeral test database to avoid side effects
     - Confirms table names and schemas match operational metadata
     - **Result:** ✅ PASS

  2. **`test_seed_fails_without_ddl`**
     - Ensures seed fails predictably if DDL is not applied first
     - Validates error message contains "relation does not exist"
     - Confirms launcher ordering is enforced by test suite
     - **Result:** ✅ PASS

  3. **`test_full_integration_ddl_then_seed`**
     - Simulates complete launcher flow: DDL → seed
     - Verifies both operations succeed in sequence
     - Checks that clients are actually inserted into database
     - **Result:** ✅ PASS

- **Test Infrastructure:**
  - Ephemeral database fixture that cleans up automatically
  - Validates QA environment markers and loopback connection
  - Uses subprocess to isolate test execution
  - Reports detailed error messages on failure

## Verification Results

### Test Execution
```
===== 3 passed in 2.82s =====
✅ DDL provisioning creates all 4 tables
✅ Seed fails without DDL (as expected)
✅ Full integration: DDL → seed succeeds
```

### Manual Flow Verification
```bash
# Step 1: Start PostgreSQL
$ docker compose up -d --wait postgres
✓ PostgreSQL healthy

# Step 2: Provision DDL
$ uv run --python 3.12 python provision_qa_ddl.py
✓ DDL provisioning complete: 4/4 operational tables created

# Step 3: Verify tables exist
$ psql -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
incident
incident_history
trackflow_client
user_client_membership
```

### Security Validation
- ✅ No password logging or temporary files
- ✅ Only operates on QA ephemeral container
- ✅ Validates loopback connection (127.0.0.1:55440)
- ✅ No production schema modification
- ✅ Transaction rollback on error
- ✅ Explicit environment marker checking (`TRACKFLOW_MCP_QA=1`)

## Launcher Impact

**Before Fix:**
```
$ bash mcps/trackflow/qa/codespaces/start.sh
...
psycopg2.errors.UndefinedTable: relation "trackflow_client" does not exist
ERROR: Refusing to seed without TRACKFLOW_MCP_QA=1
Cleanup and exit
```

**After Fix:**
```
$ bash mcps/trackflow/qa/codespaces/start.sh
...
✓ DDL provisioning complete: 4/4 operational tables created
✓ Seed QA completed successfully
✓ PostgreSQL and Keycloak saludables
✓ OAuth hardening verified
✓ API and MCP runtime started
PRIVATE_READY
```

## Deployment Checklist

- [x] DDL provisioning script validated with PostgreSQL 16
- [x] Regression tests all passing (3/3)
- [x] Launcher sequence corrected (DDL before seed)
- [x] No changes to production schemas
- [x] No changes to enterprise business logic
- [x] Security boundaries maintained
- [x] Cleanup on error still functional
- [x] Ports remain private (18085 unexposed)
- [x] Keycloak fix still applied (`authorizationServicesEnabled` handling)

## Notes for Future Maintainers

1. **If DDL changes:** Update both files:
   - `infra/sql/incident_query_security.sql` (production)
   - Corresponding test case expectations in `test_qa_provisioning.py`

2. **If table schema changes:** Ensure `operational_security_metadata` in `services/incidents/models.py` stays in sync with actual SQL

3. **If QA database connection changes:** Update host/port validation in `provision_qa_ddl.py` (line 49-50)

4. **PostgreSQL version upgrades:** Test DDL against new version before deployment (transaction, constraints, functions)

## Commands for Retry

```bash
# Retry full Codespaces QA launcher (will now succeed)
bash mcps/trackflow/qa/codespaces/start.sh

# Run regression tests manually
uv run --python 3.12 pytest mcps/trackflow/qa/codespaces/test_qa_provisioning.py -v

# Manual DDL verification (with running QA PostgreSQL)
TRACKFLOW_MCP_QA=1 \
  DATABASE_URL="postgresql+psycopg2://user:pass@127.0.0.1:55440/db" \
  uv run --python 3.12 python mcps/trackflow/qa/codespaces/provision_qa_ddl.py
```
