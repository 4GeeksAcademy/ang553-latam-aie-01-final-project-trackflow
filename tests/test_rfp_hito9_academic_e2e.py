"""Opt-in isolated academic E2E for Hito 9 across Parts 1, 2 and 3.

The application, PostgreSQL, Redis, relay, Celery workers, LangGraph workflows,
HTTP authorization and persistence are real.  Only the Part 1 LLM boundary is
the controlled local stub introduced with PR #38.  This module never calls
FourGeeks and never targets an existing TrackFlow environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from uuid import uuid4

import pytest

from data.pipelines.rfp_response.hashing import canonical_hash
from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_intake.pdf_conversion import pdf_to_markdown
from data.pipelines.rfp_intake.readability import compute_readability
from data.pipelines.rfp_response.contracts import RFPResponseStart, RequestedAspect
from data.pipelines.rfp_response.evaluators import evaluate_department
from data.pipelines.rfp_response.generators import generate_department_draft
from pipelines.rfp_approval.contracts import (
    INDICATIVE_COMMERCIAL_INPUT_CODES,
    indicative_warnings_are_reviewable,
)
from services.rfp_response.store import _response_language_from_intake
from tests.test_rfp_e2e_isolated import (
    FIXTURES,
    ROOT,
    STUB_CODE,
    _container_logs,
    _docker,
    _postgres_ready,
    _run_container,
    _wait_until,
)


RUN_ENV = "TRACKFLOW_HITO9_ACADEMIC_E2E_AUTHORIZED"
API_IMAGE_ENV = "TRACKFLOW_HITO9_ACADEMIC_E2E_API_IMAGE"
WORKER_IMAGE_ENV = "TRACKFLOW_HITO9_ACADEMIC_E2E_WORKER_IMAGE"
EVIDENCE_DIR_ENV = "TRACKFLOW_HITO9_ACADEMIC_E2E_EVIDENCE_DIR"
DIAGNOSTIC_ONLY_ENV = "TRACKFLOW_HITO9_ACADEMIC_E2E_INITIAL_STATE_ONLY"
FIXTURE = FIXTURES / "trackflow-request-1.pdf"
FIXTURE_SHA256 = "6e5ccfb2bc31ee846a8a33d853076e79824b09e387b4a8c98530626b59e2f37e"
MIGRATIONS = (
    ROOT / "infra/sql/rfp_intake.sql",
    ROOT / "infra/sql/rfp_sse.sql",
    ROOT / "infra/sql/rfp_response_generation.sql",
    ROOT / "infra/sql/rfp_approval.sql",
    ROOT / "infra/sql/rfp_approval_extension.sql",
    ROOT / "infra/sql/rfp_approval_monthly_storage_volume.sql",
    ROOT / "infra/sql/rfp_approval_indicative_validation_bundle.sql",
)

ACADEMIC_MODAVIVA_ASPECTS = {
    "warehouse": {
        "line_needle": "Almacenamiento de aproximadamente",
        "fact": (
            "Almacenamiento de aproximadamente 15.000 SKUs en un centro "
            "cercano a Zaragoza"
        ),
        "requested_action": (
            "Describir la respuesta de almacenamiento para el inventario de "
            "ModaViva cerca de Zaragoza."
        ),
    },
    "reverse": {
        "line_needle": "restock",
        "fact": (
            "Gestion de devoluciones: inspeccion, reacondicionamiento y restock"
        ),
        "requested_action": (
            "Describir la inspeccion y clasificacion de devoluciones y su "
            "reincorporacion al inventario."
        ),
    },
}


def _academic_stub_code() -> str:
    helper_anchor = "failed_modaviva=False\n"
    helper = (
        helper_anchor
        + "academic_modaviva_aspects="
        + repr(ACADEMIC_MODAVIVA_ASPECTS)
        + "\n"
        + "def numbered_line(user,needle):\n"
        + " for raw in user.splitlines():\n"
        + "  match=re.match(r'^L(\\d+):\\s?(.*)$',raw)\n"
        + "  if match and needle.casefold() in match.group(2).casefold():\n"
        + "   return int(match.group(1))\n"
        + " raise ValueError('fixture evidence line not found')\n"
    )
    historical = ''' if name.startswith("trackflow_rfp_") and name.endswith("_section"):
  department=name[len("trackflow_rfp_"):-len("_section")]
  match=re.search(r"(?m)^L(\\d+):",user)
  line=int(match.group(1)) if match else 1
  return json.dumps({"department_id":department,"key_aspects":[{"fact":"Controlled local E2E fact",
   "source_ref":ref(line),"requested_action":"Confirm operational response"}],
   "open_questions":[],"source_references":[ref(line)],"detected_conflicts":[]})
'''
    academic = ''' if name.startswith("trackflow_rfp_") and name.endswith("_section"):
  department=name[len("trackflow_rfp_"):-len("_section")]
  if "MV-LOG-2026-03" in user and department in academic_modaviva_aspects:
   aspect=academic_modaviva_aspects[department]
   line=numbered_line(user,aspect["line_needle"])
   return json.dumps({"department_id":department,"key_aspects":[{
    "fact":aspect["fact"],"source_ref":ref(line),
    "requested_action":aspect["requested_action"]}],"open_questions":[],
    "source_references":[ref(line)],"detected_conflicts":[]})
  match=re.search(r"(?m)^L(\\d+):",user)
  line=int(match.group(1)) if match else 1
  return json.dumps({"department_id":department,"key_aspects":[{"fact":"Controlled local E2E fact",
   "source_ref":ref(line),"requested_action":"Confirm operational response"}],
   "open_questions":[],"source_references":[ref(line)],"detected_conflicts":[]})
'''
    if STUB_CODE.count(helper_anchor) != 1 or STUB_CODE.count(historical) != 1:
        raise RuntimeError("historical PR #38 stub contract changed")
    return STUB_CODE.replace(helper_anchor, helper, 1).replace(
        historical, academic, 1
    )


ACADEMIC_STUB_CODE = _academic_stub_code()


def _psql(container: str, sql: str) -> str:
    """Read PostgreSQL JSON output without the Windows locale altering Unicode."""

    result = subprocess.run(
        [
            "docker", "exec", container, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", "trackflow_rfp_test", "-d", "trackflow_rfp_test", "-At",
            "-c", sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=True,
        timeout=180,
    )
    return result.stdout.strip()


class AcademicE2EConfigurationError(RuntimeError):
    pass


class _RedactedSecret(str):
    def __repr__(self) -> str:
        return "<redacted>"


def _generate_academic_secret(prefix: str) -> _RedactedSecret:
    value = _RedactedSecret(prefix + secrets.token_urlsafe(32))
    if len(value.encode("utf-8")) > 72:
        raise AcademicE2EConfigurationError("generated academic secret exceeds 72 bytes")
    return value


class AcademicE2EMigrationError(RuntimeError):
    def __init__(
        self, *, migration: str, exit_code: int, sqlstate: str | None,
        first_cause: str, raw_stderr_path: Path,
    ) -> None:
        self.migration = migration
        self.exit_code = exit_code
        self.sqlstate = sqlstate
        self.first_cause = first_cause
        self.raw_stderr_path = raw_stderr_path
        state = sqlstate if sqlstate is not None else "not_provided"
        super().__init__(
            f"migration {migration} failed: psql exit code {exit_code}; "
            f"SQLSTATE={state}; first_cause={first_cause}"
        )


class AcademicE2EBootstrapError(RuntimeError):
    def __init__(
        self, *, exit_code: int, first_cause: str, raw_stderr_path: Path,
    ) -> None:
        self.exit_code = exit_code
        self.first_cause = first_cause
        self.raw_stderr_path = raw_stderr_path
        super().__init__(
            f"identity bootstrap failed: exit code {exit_code}; "
            f"first_cause={first_cause}"
        )


class AcademicE2EClientError(RuntimeError):
    def __init__(
        self, *, exit_code: int, first_cause: str, raw_stderr_path: Path,
    ) -> None:
        self.exit_code = exit_code
        self.first_cause = first_cause
        self.raw_stderr_path = raw_stderr_path
        super().__init__(
            f"academic client failed: exit code {exit_code}; "
            f"first_cause={first_cause}"
        )


_SENSITIVE_DIAGNOSTIC_PATTERNS = (
    (
        re.compile(
            r"(?i)(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:\s/]+:)[^@\s]+(@)"
        ),
        r"\1[REDACTED]\2",
    ),
    (
        re.compile(
            r"(?i)(\b(?:password|token|secret|api[_-]?key)\s*[=:]\s*)\S+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)\S+"),
        r"\1[REDACTED]",
    ),
)
_SQLSTATE_PATTERN = re.compile(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5}):")


def _sanitize_psql_stderr(stderr: bytes) -> str:
    sanitized = stderr.decode("utf-8", errors="replace")
    for pattern, replacement in _SENSITIVE_DIAGNOSTIC_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _bootstrap_first_cause(sanitized: str) -> str:
    lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
    exception_lines = [
        line for line in lines
        if re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):", line)
    ]
    if exception_lines:
        return exception_lines[-1]
    return lines[-1] if lines else "identity_bootstrap_failed_without_stderr"


def _celery_worker_ready(container: str) -> bool:
    return "ready" in _container_logs([container]).casefold()


def _apply_migration(
    *, container: str, migration: Path, evidence_dir: Path, evidence_prefix: str,
) -> None:
    payload = migration.read_bytes()
    command = [
        "docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1",
        "-v", "VERBOSITY=verbose", "-U", "trackflow_rfp_test", "-d",
        "trackflow_rfp_test",
    ]
    result = subprocess.run(
        command, input=payload, capture_output=True, text=False, check=False,
        timeout=180,
    )
    if result.returncode == 0:
        return

    raw_path = evidence_dir / f"{evidence_prefix}-{migration.name}-psql.stderr.raw"
    sanitized_path = (
        evidence_dir / f"{evidence_prefix}-{migration.name}-psql.stderr.sanitized.txt"
    )
    raw_path.write_bytes(result.stderr)
    sanitized = _sanitize_psql_stderr(result.stderr)
    sanitized_path.write_text(sanitized, encoding="utf-8")
    sqlstate_match = _SQLSTATE_PATTERN.search(sanitized)
    first_cause = next(
        (line.strip() for line in sanitized.splitlines() if line.strip()),
        "psql_failed_without_stderr",
    )
    error = AcademicE2EMigrationError(
        migration=migration.name,
        exit_code=result.returncode,
        sqlstate=sqlstate_match.group(1) if sqlstate_match else None,
        first_cause=first_cause,
        raw_stderr_path=raw_path,
    )
    cause = subprocess.CalledProcessError(
        result.returncode, command, output=result.stdout, stderr=result.stderr,
    )
    raise error from cause


def _run_identity_seed(
    *, network: str, auth_volume: str, image: str, database_url: str,
    academic_password: str, jwt_secret: str, evidence_dir: Path,
    evidence_prefix: str,
) -> None:
    command = [
        "docker", "run", "--rm", "--network", network,
        "--volume", f"{auth_volume}:/app/services/api/data",
        "--env", "DATABASE_URL",
        "--env", "ACADEMIC_E2E_PASSWORD",
        "--env", "JWT_SECRET_KEY",
        "--env", "TRACKFLOW_AUTH_DATABASE_PATH=/app/services/api/data/auth.json",
        image, "python", "-c", IDENTITY_SEED_CODE,
    ]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "ACADEMIC_E2E_PASSWORD": academic_password,
        "JWT_SECRET_KEY": jwt_secret,
    }
    result = subprocess.run(
        command, capture_output=True, text=False, check=False, timeout=180,
        env=environment,
    )
    if result.returncode == 0:
        return

    raw_path = evidence_dir / f"{evidence_prefix}-bootstrap.stderr.raw"
    sanitized_path = evidence_dir / f"{evidence_prefix}-bootstrap.stderr.sanitized.txt"
    raw_path.write_bytes(result.stderr)
    sanitized = _sanitize_psql_stderr(result.stderr)
    sanitized_path.write_text(sanitized, encoding="utf-8")
    first_cause = _bootstrap_first_cause(sanitized)
    error = AcademicE2EBootstrapError(
        exit_code=result.returncode,
        first_cause=first_cause,
        raw_stderr_path=raw_path,
    )
    cause = subprocess.CalledProcessError(
        result.returncode, command, output=result.stdout, stderr=result.stderr,
    )
    raise error from cause


def _run_academic_client(
    *, container: str, evidence_dir: Path, evidence_prefix: str,
) -> str:
    command = ["docker", "exec", container, "python", "-c", CLIENT_CODE]
    result = subprocess.run(
        command, capture_output=True, text=False, check=False, timeout=300,
    )
    if result.returncode == 0:
        return result.stdout.decode("utf-8", errors="strict").strip()

    raw_path = evidence_dir / f"{evidence_prefix}-client.stderr.raw"
    sanitized_path = evidence_dir / f"{evidence_prefix}-client.stderr.sanitized.txt"
    raw_path.write_bytes(result.stderr)
    sanitized = _sanitize_psql_stderr(result.stderr)
    sanitized_path.write_text(sanitized, encoding="utf-8")
    error = AcademicE2EClientError(
        exit_code=result.returncode,
        first_cause=_bootstrap_first_cause(sanitized),
        raw_stderr_path=raw_path,
    )
    cause = subprocess.CalledProcessError(
        result.returncode, command, output=result.stdout, stderr=result.stderr,
    )
    raise error from cause


def _initial_revision_reason_codes(item: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    if item.get("mandatory_compliance_passed") is not True:
        reasons.append("mandatory_compliance_failed")
    if item.get("blocking_rule_ids"):
        reasons.append("blocking_rules_present")
    unsupported = set(item.get("unresolved_input_codes") or ()) - set(
        INDICATIVE_COMMERCIAL_INPUT_CODES
    )
    if unsupported:
        reasons.append("unresolved_input_codes_not_reviewable")
    if reasons:
        return reasons
    if item.get("status") == "pending_assessment" and item.get("eligibility") is True:
        return ["eligible_pending_conflict_assessment"]
    if item.get("status") == "blocked_compliance":
        return ["blocked_compliance_without_exposed_cause"]
    return ["status_requires_contract_review"]


def _capture_initial_revision_diagnostics(
    *, container: str, evidence_dir: Path, evidence_prefix: str,
) -> str:
    rows = json.loads(_psql(container, """
      SELECT COALESCE(json_agg(json_build_object(
        'department_id',revision.department_id,
        'revision_id',revision.revision_id,
        'status',revision.status,
        'eligibility',bundle.eligible,
        'eligible_for_approval',
          revision.validation_payload->'eligible_for_approval',
        'mandatory_compliance_passed',
          revision.validation_payload->'mandatory_compliance_passed',
        'blocking_rule_ids',bundle.blocking_rule_ids,
        'unresolved_input_codes',bundle.unresolved_input_codes,
        'readability_passed',part2.payload->'readability'->'pass',
        'relevance_passed',part2.payload->'relevance'->'pass',
        'compliance_passed',part2.payload->'compliance'->'pass',
        'part2_overall_pass',part2.overall_pass,
        'part2_terminal_status',part2.terminal_status,
        'safe_preview_gate_status',preview.payload->>'gate_status'
      ) ORDER BY revision.department_id),'[]'::json)
      FROM rfp_approval_department_revisions AS revision
      JOIN rfp_approval_validation_bundles AS bundle
        ON bundle.validation_bundle_id=revision.validation_bundle_id
       AND bundle.approval_run_id=revision.approval_run_id
       AND bundle.ticket_id=revision.ticket_id
       AND bundle.scope_key=revision.department_id
      JOIN rfp_response_evaluations AS part2
        ON part2.evaluation_id=revision.source_part2_evaluation_id
      JOIN rfp_approval_validation_artifacts AS preview
        ON preview.artifact_id=bundle.safe_preview_artifact_id
       AND preview.approval_run_id=bundle.approval_run_id
       AND preview.ticket_id=bundle.ticket_id
       AND preview.scope_key=bundle.scope_key
       AND preview.artifact_type='safe_preview'
      WHERE revision.is_current IS TRUE
    """))
    diagnostics = []
    for row in rows:
        diagnostics.append(
            {
                **row,
                "stage": "approval_run_initialized_before_conflict_assessment",
                "contract_reason_codes": _initial_revision_reason_codes(row),
            }
        )
    path = evidence_dir / f"{evidence_prefix}-initial-revisions.sanitized.json"
    path.write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8",
    )
    return path.name


@dataclass(frozen=True, slots=True)
class AcademicE2ESettings:
    api_image: str
    worker_image: str
    evidence_dir: Path


def _settings(source: dict[str, str] | None = None) -> AcademicE2ESettings:
    values = os.environ if source is None else source
    if values.get(RUN_ENV) != "YES":
        raise AcademicE2EConfigurationError(f"{RUN_ENV}=YES is required")
    api_image = values.get(API_IMAGE_ENV, "").strip()
    worker_image = values.get(WORKER_IMAGE_ENV, "").strip()
    if not api_image or not worker_image:
        raise AcademicE2EConfigurationError("fresh API and worker images are required")
    raw_evidence = values.get(EVIDENCE_DIR_ENV, "")
    evidence_dir = Path(raw_evidence)
    if not evidence_dir.is_absolute():
        raise AcademicE2EConfigurationError("evidence directory must be absolute")
    resolved = evidence_dir.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise AcademicE2EConfigurationError("evidence directory must be outside repository")
    if not resolved.is_dir():
        raise AcademicE2EConfigurationError("evidence directory must already exist")
    return AcademicE2ESettings(api_image, worker_image, resolved)


def _image_id(image: str) -> str:
    inspection = json.loads(_docker("image", "inspect", image, check=True).stdout)[0]
    image_id = inspection["Id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise AssertionError("Docker image did not resolve to an immutable id")
    if inspection["Config"]["User"] != "10002:10002":
        raise AssertionError("application image must run as the reviewed non-root user")
    return image_id


def _prepare_checkpoint_volume(*, volume: str, image: str) -> None:
    """Make only the disposable checkpoint volume writable by the app user."""
    initialization = (
        "import os,pathlib,stat;"
        "p=pathlib.Path('/app/runtime');"
        "p.mkdir(parents=True,exist_ok=True);"
        "os.chown(p,10002,10002);"
        "os.chmod(p,0o750);"
        "s=p.stat();"
        "assert (s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))==(10002,10002,0o750)"
    )
    _docker(
        "run", "--rm", "--user", "0:0",
        "--volume", f"{volume}:/app/runtime",
        image, "python", "-c", initialization,
        check=True,
    )


IDENTITY_SEED_CODE = r'''
import hashlib
from datetime import datetime,timezone,timedelta
from uuid import UUID
from sqlalchemy import create_engine,insert
from sqlmodel import Session
from services.api.auth_database import users
from services.api.auth_models import Role,UserInDB
from services.api.auth_security import hash_password
from services.rfp_approval.models import rfp_principals,rfp_identity_bindings,rfp_authorizations

password=__import__("os").environ["ACADEMIC_E2E_PASSWORD"]
accounts=(
 ("academic-admin","academic-admin@trackflow.test",Role.admin),
 ("academic-miguel","academic-miguel@trackflow.test",Role.manager),
 ("academic-ana","academic-ana@trackflow.test",Role.user),
 ("academic-sofia","academic-sofia@trackflow.test",Role.user),
)
if len(users): raise RuntimeError("isolated auth store was not empty")
for user_id,email,role in accounts:
 users.insert(UserInDB(id=user_id,email=email,hashed_password=hash_password(password),role=role).model_dump(mode="json"))

specs=(
 ("10000000-0000-4000-8000-000000000001","20000000-0000-4000-8000-000000000001","academic-miguel","miguel_torres","30000000-0000-4000-8000-000000000001","manage_approval_run",None,"run"),
 ("10000000-0000-4000-8000-000000000002","20000000-0000-4000-8000-000000000002","academic-ana","ana_whitfield","30000000-0000-4000-8000-000000000002","approve_department","warehouse","department_exact"),
 ("10000000-0000-4000-8000-000000000003","20000000-0000-4000-8000-000000000003","academic-sofia","sofia_ramos","30000000-0000-4000-8000-000000000003","approve_department","reverse","department_exact"),
)
engine=create_engine(__import__("os").environ["DATABASE_URL"])
with Session(engine) as session,session.begin():
 for principal,binding,user_id,responsible,authorization,permission,department,target in specs:
  principal_id=UUID(principal); binding_id=UUID(binding); authorization_id=UUID(authorization)
  session.execute(insert(rfp_principals).values(principal_id=principal_id,environment="qa",trackflow_user_id=user_id,responsible_code=responsible,synthetic=True))
  session.execute(insert(rfp_identity_bindings).values(binding_id=binding_id,principal_id=principal_id,environment="qa",auth_source="local",issuer="urn:trackflow:qa:local",subject=user_id))
  session.execute(insert(rfp_authorizations).values(authorization_id=authorization_id,principal_id=principal_id,environment="qa",permission=permission,department_scope=department,target_policy=target,scope_fingerprint=hashlib.sha256(("academic-e2e:"+authorization).encode()).hexdigest(),authorization_version=1,active_from=datetime.now(timezone.utc)-timedelta(minutes=1),granted_by_principal_id=principal_id))
engine.dispose()
'''


CLIENT_CODE = r'''
import json,os,pathlib,time
from uuid import uuid4
import httpx

password=os.environ["ACADEMIC_E2E_PASSWORD"]
diagnostic_only=os.environ.get("TRACKFLOW_HITO9_ACADEMIC_E2E_INITIAL_STATE_ONLY")=="YES"
def payload(response,expected=200):
 if response.status_code!=expected: raise RuntimeError(f"unexpected HTTP status {response.status_code} at {response.request.url.path}")
 value=response.json()
 if not isinstance(value,dict): raise RuntimeError("HTTP response was not an object")
 return value
def wait_get(client,path,headers,statuses,timeout=180):
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  response=client.get(path,headers=headers)
  if response.status_code==200:
   value=response.json()
   if value.get("status") in statuses: return value
  time.sleep(.5)
 raise RuntimeError(f"timed out waiting for {path}")

with httpx.Client(base_url="http://127.0.0.1:8000",timeout=30) as client:
 tokens={}
 for actor in ("admin","miguel","ana","sofia"):
  login=client.post("/auth/login",data={"username":f"academic-{actor}@trackflow.test","password":password})
  tokens[actor]=payload(login)["access_token"]
 headers={actor:{"Authorization":"Bearer "+token} for actor,token in tokens.items()}
 fixture=pathlib.Path("/e2e-fixtures/trackflow-request-1.pdf")
 with fixture.open("rb") as source:
  accepted=payload(client.post("/rfps",headers=headers["admin"],files={"file":(fixture.name,source,"application/pdf")}),202)
 ticket_id=accepted["ticket_id"]
 intake=wait_get(client,f"/rfps/{ticket_id}",headers["admin"],{"analisis_completo"})
 assert intake["job"]["status"]=="succeeded"
 assert intake["metadata"]["client_country_code"]=="ES"
 assert intake["metadata"]["currency"]=="EUR"
 assert set(intake["metadata"]["departments_needed"])=={"warehouse","reverse"}
 assert [(x["service_id"],x["requested"]) for x in intake["metadata"]["services_requested"]]==[("warehouse",True),("lastmile",False),("reverse",True)]
 assert {x["department_id"] for x in intake["department_sections"]}=={"warehouse","reverse"}
 assert all(x["key_aspects"] for x in intake["department_sections"])
 assert intake["handoff"]["contract_version"]=="rfp-intake-v1"

 response_start=payload(client.post(f"/rfps/{ticket_id}/response",headers=headers["admin"],json={"carrier_model":"unknown"}),202)
 response_run_id=response_start["run_id"]
 response=wait_get(client,f"/rfps/{ticket_id}/response/{response_run_id}",headers["admin"],{"needs_human_review","ready_for_part3","technical_failure"})
 assert response["status"]=="needs_human_review"
 assert response["outcome"]=="needs_human_review"
 assert response["technical_failure"] is False

 initialized=payload(client.post(f"/rfps/{ticket_id}/approval",headers=headers["miguel"],json={"idempotency_key":str(uuid4()),"automate_proposal_materialization":True}))
 approval_run_id=initialized["approval_run_id"]
 assert initialized["response_run_id"]==response_run_id
 assert initialized["proposal_automation_delegated"] is True
 revisions={item["department"]:item for item in initialized["revisions"]}
 assert set(revisions)=={"warehouse","reverse"}
 initial_revision_response={department:{"revision_id":item["revision_id"],"status":item["status"],"validation_bundle_id":item["validation_bundle_id"],"ready_for_hitl":item["ready_for_hitl"]} for department,item in revisions.items()}
 if diagnostic_only:
  print(json.dumps({"ticket_id":ticket_id,"response_run_id":response_run_id,"approval_run_id":approval_run_id,"initial_revisions":initial_revision_response,"part1_provider":"controlled_local_stub"},sort_keys=True))
  raise SystemExit(0)
 if {item["status"] for item in revisions.values()}!={"pending_assessment"}:
  import sys
  print("initial_revision_response_diagnostic="+json.dumps(initial_revision_response,sort_keys=True),file=sys.stderr)
 assert {item["status"] for item in revisions.values()}=={"pending_assessment"}

 assessed=payload(client.post(f"/rfps/{ticket_id}/approval/{approval_run_id}/assess-and-start",headers=headers["miguel"]))
 ready={item["department"]:item for item in assessed["ready_revisions"]}
 assert set(ready)=={"warehouse","reverse"}
 base=f"/rfps/{ticket_id}/approval/{approval_run_id}"
 reviews={}
 workflows={}
 for department,actor in (("warehouse","ana"),("reverse","sofia")):
  revision_id=ready[department]["revision_id"]
  reviews[department]=payload(client.get(f"{base}/departments/{department}/revisions/{revision_id}/review",headers=headers[actor]))
  workflows[department]=payload(client.get(f"{base}/departments/{department}/revisions/{revision_id}",headers=headers[actor]))
  assert workflows[department]["status"]=="awaiting_human_decision"
  assert workflows[department]["interrupted"] is True
 assert workflows["warehouse"]["thread_id"]!=workflows["reverse"]["thread_id"]
 assert reviews["warehouse"]["pending_commercial_items"]==[]
 reverse_codes={x["code"] for x in reviews["reverse"]["pending_commercial_items"]}
 assert reverse_codes=={"RETURN_PROCESSING_RATE_REQUIRED"}

 warehouse_url=f"{base}/departments/warehouse/revisions/{ready['warehouse']['revision_id']}/decision"
 reverse_url=f"{base}/departments/reverse/revisions/{ready['reverse']['revision_id']}/decision"
 no_ack=client.post(reverse_url,headers=headers["sofia"],json={"action":"approve","reason":None})
 assert no_ack.status_code==422 and no_ack.json()["detail"]=="pending_commercial_items_acknowledgement_required"
 crossed=client.post(reverse_url,headers=headers["ana"],json={"action":"approve","acknowledge_pending_commercial_items":True,"reason":"Controlled academic acknowledgement."})
 assert crossed.status_code==403

 ana=payload(client.post(warehouse_url,headers=headers["ana"],json={"action":"approve","reason":None}))
 assert ana["application"]["revision_status"]=="approved"
 assert ana["coordination"]["outcome"]=="waiting"
 assert client.get(f"{base}/document",headers=headers["miguel"]).status_code==404
 reverse_waiting=payload(client.get(f"{base}/departments/reverse/revisions/{ready['reverse']['revision_id']}",headers=headers["sofia"]))
 assert reverse_waiting["interrupted"] is True and reverse_waiting["decision"] is None

 sofia=payload(client.post(reverse_url,headers=headers["sofia"],json={"action":"approve","acknowledge_pending_commercial_items":True,"reason":"Controlled academic acknowledgement."}))
 assert sofia["application"]["revision_status"]=="approved"
 assert sofia["coordination"]["outcome"]=="finalized"
 final_document_id=sofia["coordination"]["final_document_id"]
 document=payload(client.get(f"{base}/document",headers=headers["miguel"]))
 assert document["final_document_id"]==final_document_id
 assert document["title"].startswith("TrackFlow indicative logistics proposal")
 assert "Indicative, non-binding proposal" in document["markdown"]
 assert document["currency"]=={"value":"EUR","status":"documented"}
 assert {x["department"] for x in document["sections"]}=={"warehouse","reverse"}
 assert "RETURN_PROCESSING_RATE_REQUIRED" in document["markdown"]
 assert "MONTHLY_STORAGE_VOLUME_REQUIRED" in document["markdown"]
 assert "Pending \u2014 no percentage authorized" in document["markdown"]
 assert "None%" not in document["markdown"] and "| 0% |" not in document["markdown"]
 terminal=payload(client.get(f"/rfps/{ticket_id}",headers=headers["admin"]))
 assert terminal["status"]=="terminado"

 replay=client.post(reverse_url,headers=headers["sofia"],json={"action":"approve","acknowledge_pending_commercial_items":True,"reason":"Controlled academic acknowledgement."})
 assert replay.status_code==409 and replay.json()["detail"]=="workflow_not_interrupted"
 reconciled=payload(client.post(f"{base}/reconcile",headers=headers["miguel"],json={"revision_id":ready["warehouse"]["revision_id"],"department":"warehouse"}))
 assert reconciled["coordination"]["outcome"]=="finalized"
 assert reconciled["coordination"]["final_document_id"]==final_document_id
 assert payload(client.get(f"{base}/document",headers=headers["miguel"]))==document
 print(json.dumps({"ticket_id":ticket_id,"response_run_id":response_run_id,"approval_run_id":approval_run_id,"final_document_id":final_document_id,"revision_ids":{k:v["revision_id"] for k,v in ready.items()},"thread_ids":{k:v["thread_id"] for k,v in workflows.items()},"pending_codes":{"reverse":sorted(reverse_codes),"global":["MONTHLY_STORAGE_VOLUME_REQUIRED"]},"part1_provider":"controlled_local_stub"},sort_keys=True))
'''


def test_academic_e2e_guardrails_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AcademicE2EConfigurationError, match="=YES"):
        _settings({})
    values = {
        RUN_ENV: "YES",
        API_IMAGE_ENV: "api-image",
        WORKER_IMAGE_ENV: "worker-image",
        EVIDENCE_DIR_ENV: str(ROOT),
    }
    with pytest.raises(AcademicE2EConfigurationError, match="outside repository"):
        _settings(values)
    values[EVIDENCE_DIR_ENV] = str(tmp_path)
    assert _settings(values).evidence_dir == tmp_path.resolve()


def test_academic_secrets_are_secure_bcrypt_compatible_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets, "token_urlsafe", lambda count: "A" * 43)
    value = _generate_academic_secret("AcademicUser-")
    assert isinstance(value, str)
    assert len(value.encode("utf-8")) == 56
    assert len(value.encode("utf-8")) <= 72
    assert repr(value) == "<redacted>"


def test_academic_e2e_reuses_pr38_stub_and_official_modaviva_pdf() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    assert "from tests.test_rfp_e2e_isolated import" in source
    assert "STUB_CODE" in source
    assert "controlled_local_stub" in source
    assert "RFP_RESPONSE_LLM_MODE\": \"deterministic" in source
    assert "postgresql+psycopg2://trackflow_rfp_test" in source
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert all(path.is_file() and path.stat().st_size > 0 for path in MIGRATIONS)
    assert [path.name for path in MIGRATIONS] == [
        "rfp_intake.sql",
        "rfp_sse.sql",
        "rfp_response_generation.sql",
        "rfp_approval.sql",
        "rfp_approval_extension.sql",
        "rfp_approval_monthly_storage_volume.sql",
        "rfp_approval_indicative_validation_bundle.sql",
    ]


def test_academic_modaviva_stub_is_opt_in_and_uses_bounded_pdf_facts() -> None:
    historical_namespace: dict[str, object] = {}
    academic_namespace: dict[str, object] = {}
    exec(STUB_CODE.split("class Handler", 1)[0], historical_namespace)
    exec(ACADEMIC_STUB_CODE.split("class Handler", 1)[0], academic_namespace)
    extract = "\n".join(
        (
            'metadata={"rfp_id":"MV-LOG-2026-03"}',
            "L21: Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano a Zaragoza",
            "L23: Gestion de devoluciones: inspeccion, reacondicionamiento y restock",
        )
    )
    historical = json.loads(
        historical_namespace["content_for"](
            "trackflow_rfp_warehouse_section", extract
        )
    )
    warehouse = json.loads(
        academic_namespace["content_for"](
            "trackflow_rfp_warehouse_section", extract
        )
    )
    reverse = json.loads(
        academic_namespace["content_for"](
            "trackflow_rfp_reverse_section", extract
        )
    )

    assert historical["key_aspects"][0]["fact"] == "Controlled local E2E fact"
    assert warehouse["key_aspects"] == [{
        "fact": ACADEMIC_MODAVIVA_ASPECTS["warehouse"]["fact"],
        "source_ref": {"line_start": 21, "line_end": 21},
        "requested_action": ACADEMIC_MODAVIVA_ASPECTS["warehouse"][
            "requested_action"
        ],
    }]
    assert reverse["key_aspects"] == [{
        "fact": ACADEMIC_MODAVIVA_ASPECTS["reverse"]["fact"],
        "source_ref": {"line_start": 23, "line_end": 23},
        "requested_action": ACADEMIC_MODAVIVA_ASPECTS["reverse"][
            "requested_action"
        ],
    }]
    serialized = json.dumps(
        {"warehouse": warehouse, "reverse": reverse}, ensure_ascii=False
    ).casefold()
    assert "return_processing_rate" not in serialized
    assert "m3" not in serialized
    assert "discount" not in serialized
    assert "capacidad" not in serialized


def test_academic_modaviva_aspects_use_real_evaluators_without_forcing_pass() -> None:
    departments = (DepartmentId.warehouse, DepartmentId.reverse)
    aspects = [
        RequestedAspect(
            aspect_id="warehouse:01:21-21",
            department=DepartmentId.warehouse,
            fact=ACADEMIC_MODAVIVA_ASPECTS["warehouse"]["fact"],
            requested_action=ACADEMIC_MODAVIVA_ASPECTS["warehouse"][
                "requested_action"
            ],
            line_start=21,
            line_end=21,
        ),
        RequestedAspect(
            aspect_id="reverse:01:23-23",
            department=DepartmentId.reverse,
            fact=ACADEMIC_MODAVIVA_ASPECTS["reverse"]["fact"],
            requested_action=ACADEMIC_MODAVIVA_ASPECTS["reverse"][
                "requested_action"
            ],
            line_start=23,
            line_end=23,
        ),
    ]

    def evaluated(language: str) -> dict[DepartmentId, tuple[object, object]]:
        request = RFPResponseStart(
            ticket_id=uuid4(),
            run_id=uuid4(),
            source_handoff_hash="a" * 64,
            summary_for_sales=(
                "ModaViva solicita servicios de almacenamiento y devoluciones "
                "en Espana."
            ),
            key_aspects=aspects,
            selected_departments=list(departments),
            client_country_code="ES",
            operation_countries=["ES"],
            proposal_currency="EUR",
            average_monthly_volume_m3=None,
            customer_managed_carrier=None,
            carrier_confirmation_hash="b" * 64,
            language=language,
        )
        evaluated_at = datetime.now(timezone.utc)
        results = {}
        for department in departments:
            draft = generate_department_draft(
                request,
                department=department,
                iteration_count=3,
                generated_at=evaluated_at,
            )
            result = evaluate_department(
                request, draft, evaluated_at=evaluated_at
            )[0]
            results[department] = (draft, result)
        return results

    english = evaluated("en")
    assert all(result.relevance.passed for _draft, result in english.values())
    assert all(result.compliance.passed for _draft, result in english.values())
    assert all(
        result.blocking_rule_ids == [] for _draft, result in english.values()
    )
    assert all(
        result.readability.passed is False
        for _draft, result in english.values()
    )

    conversion = pdf_to_markdown(
        FIXTURE,
        markdown_reference="rfp_intake/academic/source.md",
        max_pages=100,
        timeout_seconds=60,
    )
    readability_report = compute_readability(conversion.markdown)
    selected_language = _response_language_from_intake(
        readability_report.model_dump(mode="json")
    )
    assert {metric.language for metric in readability_report.metrics} == {"es"}
    assert selected_language == "es"

    spanish = evaluated(selected_language)
    warehouse_draft, warehouse = spanish[DepartmentId.warehouse]
    reverse_draft, reverse = spanish[DepartmentId.reverse]
    assert warehouse.readability.passed is True
    assert warehouse.relevance.passed is True
    assert warehouse.compliance.passed is True
    assert warehouse.overall_pass is True
    assert reverse.readability.passed is True
    assert reverse.relevance.passed is True
    assert reverse.compliance.passed is True
    assert reverse.blocking_rule_ids == []
    assert reverse.overall_pass is False
    assert reverse.terminal_status == "needs_human_input"
    assert warehouse_draft.pending_commercial_items == []
    reverse_pending = [item.code for item in reverse_draft.pending_commercial_items]
    assert reverse_pending == ["RETURN_PROCESSING_RATE_REQUIRED"]
    assert indicative_warnings_are_reviewable(
        blocking_rule_ids=warehouse.blocking_rule_ids,
        unresolved_input_codes=[],
    )
    assert indicative_warnings_are_reviewable(
        blocking_rule_ids=reverse.blocking_rule_ids,
        unresolved_input_codes=reverse_pending,
    )
    assert {
        department: (
            "pending_assessment"
            if result.readability.passed is not False
            and result.relevance.passed
            and result.compliance.passed
            else "blocked_compliance"
        )
        for department, (_draft, result) in spanish.items()
    } == {
        DepartmentId.warehouse: "pending_assessment",
        DepartmentId.reverse: "pending_assessment",
    }


def test_academic_e2e_drives_business_transitions_only_through_http() -> None:
    assert 'client.post("/rfps"' in CLIENT_CODE
    assert '/response"' in CLIENT_CODE
    assert '/approval"' in CLIENT_CODE
    assert "assess-and-start" in CLIENT_CODE
    assert "/decision" in CLIENT_CODE
    assert "/reconcile" in CLIENT_CODE
    assert "INSERT INTO rfp_response" not in IDENTITY_SEED_CODE
    assert "rfp_human_decisions" not in IDENTITY_SEED_CODE
    assert "rfp_final_documents" not in IDENTITY_SEED_CODE


def test_academic_migration_transport_preserves_utf8_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = tmp_path / "accented.sql"
    original = "SELECT 'en_evaluación', 'España', 'logística';\n".encode("utf-8")
    migration.write_bytes(original)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _apply_migration(
        container="isolated-postgres", migration=migration,
        evidence_dir=tmp_path, evidence_prefix="academic",
    )

    assert observed["input"] == original
    assert observed["text"] is False
    assert observed["check"] is False
    assert "ON_ERROR_STOP=1" in observed["command"]
    assert "VERBOSITY=verbose" in observed["command"]


def test_academic_psql_decodes_json_as_strict_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    output = '{"title":"Propuesta — España","unit":"m³"}\n'

    def fake_run(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _psql("isolated-postgres", "SELECT payload") == output.strip()
    assert observed["text"] is True
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "strict"
    assert observed["check"] is True


def test_academic_migration_failure_preserves_raw_and_sanitizes_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = tmp_path / "broken.sql"
    migration.write_bytes(b"SELECT 1;\n")
    stderr = (
        b"ERROR:  23505: duplicate key; password=private-value "
        b"postgresql://runner:database-secret@postgres/test\n"
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 3, stdout=b"", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AcademicE2EMigrationError) as captured:
        _apply_migration(
            container="isolated-postgres", migration=migration,
            evidence_dir=tmp_path, evidence_prefix="academic",
        )

    error = captured.value
    assert error.migration == migration.name
    assert error.exit_code == 3
    assert error.sqlstate == "23505"
    assert error.__cause__ is not None
    assert error.raw_stderr_path.read_bytes() == stderr
    sanitized = (tmp_path / "academic-broken.sql-psql.stderr.sanitized.txt").read_text(
        encoding="utf-8"
    )
    assert "private-value" not in sanitized
    assert "database-secret" not in sanitized
    assert sanitized.count("[REDACTED]") == 2
    assert "23505" in sanitized


def test_academic_identity_seed_inherits_secrets_without_command_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_identity_seed(
        network="isolated-network",
        auth_volume="isolated-auth",
        image="reviewed-image",
        database_url="postgresql://runner:database-secret@postgres/test",
        academic_password="academic-secret",
        jwt_secret="jwt-secret",
        evidence_dir=tmp_path,
        evidence_prefix="academic",
    )

    command = observed["command"]
    assert "DATABASE_URL" in command
    assert "ACADEMIC_E2E_PASSWORD" in command
    assert "JWT_SECRET_KEY" in command
    assert all("database-secret" not in argument for argument in command)
    assert all("academic-secret" not in argument for argument in command)
    assert all("jwt-secret" not in argument for argument in command)
    environment = observed["env"]
    assert environment["DATABASE_URL"].endswith("@postgres/test")
    assert environment["ACADEMIC_E2E_PASSWORD"] == "academic-secret"
    assert environment["JWT_SECRET_KEY"] == "jwt-secret"


def test_academic_checkpoint_volume_is_prepared_for_non_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_docker(*arguments, **kwargs):
        observed.update(arguments=arguments, kwargs=kwargs)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "tests.test_rfp_hito9_academic_e2e._docker", fake_docker,
    )
    _prepare_checkpoint_volume(
        volume="isolated-checkpoints", image="reviewed-image",
    )

    arguments = observed["arguments"]
    assert arguments[:5] == (
        "run", "--rm", "--user", "0:0", "--volume",
    )
    assert "isolated-checkpoints:/app/runtime" in arguments
    assert "reviewed-image" in arguments
    initialization = arguments[-1]
    assert "os.chown(p,10002,10002)" in initialization
    assert "os.chmod(p,0o750)" in initialization
    assert observed["kwargs"] == {"check": True}


def test_initial_revision_diagnostics_are_content_free_and_contractual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "department_id": "reverse",
            "revision_id": "10000000-0000-4000-8000-000000000001",
            "status": "blocked_compliance",
            "eligibility": False,
            "eligible_for_approval": False,
            "mandatory_compliance_passed": True,
            "blocking_rule_ids": ["returns_sla_breach"],
            "unresolved_input_codes": ["RETURN_PROCESSING_RATE_REQUIRED"],
            "readability_passed": True,
            "relevance_passed": True,
            "compliance_passed": True,
            "part2_overall_pass": False,
            "part2_terminal_status": "needs_human_input",
            "safe_preview_gate_status": "passed",
        },
        {
            "department_id": "warehouse",
            "revision_id": "10000000-0000-4000-8000-000000000002",
            "status": "pending_assessment",
            "eligibility": True,
            "eligible_for_approval": True,
            "mandatory_compliance_passed": True,
            "blocking_rule_ids": [],
            "unresolved_input_codes": [],
            "readability_passed": True,
            "relevance_passed": True,
            "compliance_passed": True,
            "part2_overall_pass": True,
            "part2_terminal_status": "passed",
            "safe_preview_gate_status": "passed",
        },
    ]
    observed: dict[str, str] = {}

    def fake_psql(container: str, sql: str) -> str:
        observed.update(container=container, sql=sql)
        return json.dumps(rows)

    monkeypatch.setattr(
        "tests.test_rfp_hito9_academic_e2e._psql", fake_psql,
    )

    filename = _capture_initial_revision_diagnostics(
        container="isolated-postgres",
        evidence_dir=tmp_path,
        evidence_prefix="academic",
    )
    diagnostics = json.loads((tmp_path / filename).read_text(encoding="utf-8"))

    assert diagnostics[0]["contract_reason_codes"] == ["blocking_rules_present"]
    assert diagnostics[1]["contract_reason_codes"] == [
        "eligible_pending_conflict_assessment"
    ]
    assert all(
        item["stage"] == "approval_run_initialized_before_conflict_assessment"
        for item in diagnostics
    )
    serialized = json.dumps(diagnostics)
    assert "generated_content" not in serialized
    assert "commercial_terms" not in serialized
    assert "password" not in serialized
    assert "bundle.scope_key=revision.department_id" in observed["sql"]
    assert "bundle.department_id" not in observed["sql"]


def test_initial_state_diagnostic_stops_before_assessment() -> None:
    diagnostic_exit = CLIENT_CODE.index("if diagnostic_only:")
    initial_assertion = CLIENT_CODE.index(
        'assert {item["status"] for item in revisions.values()}'
    )
    assessment = CLIENT_CODE.index("assess-and-start")
    assert diagnostic_exit < initial_assertion < assessment
    assert "TRACKFLOW_HITO9_ACADEMIC_E2E_INITIAL_STATE_ONLY" in CLIENT_CODE


def test_initial_state_http_evidence_precedes_independent_sql_diagnostic() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    test_start = source.index(
        "def test_modaviva_academic_pdf_reaches_indicative_final_document()"
    )
    diagnostic_branch = source.index("if diagnostic_only:", test_start)
    http_evidence = source.index(
        'f"{prefix}-initial-response.sanitized.json"', diagnostic_branch
    )
    http_write = source.index("http_diagnostics_path.write_text", http_evidence)
    independent_sql = source.index(
        "_capture_initial_revision_diagnostics(", http_write
    )
    assert diagnostic_branch < http_evidence < http_write < independent_sql


def test_academic_identity_seed_failure_preserves_sanitized_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        b"RuntimeError: password=private-value "
        b"postgresql://runner:database-secret@postgres/test\n"
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AcademicE2EBootstrapError) as captured:
        _run_identity_seed(
            network="isolated-network",
            auth_volume="isolated-auth",
            image="reviewed-image",
            database_url="postgresql://runner:database-secret@postgres/test",
            academic_password="academic-secret",
            jwt_secret="jwt-secret",
            evidence_dir=tmp_path,
            evidence_prefix="academic",
        )

    error = captured.value
    assert error.exit_code == 1
    assert error.__cause__ is not None
    assert error.raw_stderr_path.read_bytes() == stderr
    sanitized = (tmp_path / "academic-bootstrap.stderr.sanitized.txt").read_text(
        encoding="utf-8"
    )
    assert "private-value" not in sanitized
    assert "database-secret" not in sanitized
    assert sanitized.count("[REDACTED]") == 2
    assert error.first_cause.startswith("RuntimeError:")


def test_academic_celery_readiness_reads_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tests.test_rfp_hito9_academic_e2e._container_logs",
        lambda containers: "\n--- worker ---\nacademic-e2e@container ready.\n",
    )
    assert _celery_worker_ready("isolated-worker") is True


def test_academic_client_failure_preserves_sanitized_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = b"AssertionError: token=private-token\n"

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AcademicE2EClientError) as captured:
        _run_academic_client(
            container="isolated-api",
            evidence_dir=tmp_path,
            evidence_prefix="academic",
        )

    error = captured.value
    assert error.exit_code == 1
    assert error.__cause__ is not None
    assert error.raw_stderr_path.read_bytes() == stderr
    sanitized = (tmp_path / "academic-client.stderr.sanitized.txt").read_text(
        encoding="utf-8"
    )
    assert "private-token" not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.skipif(os.getenv(RUN_ENV) != "YES", reason=f"{RUN_ENV}=YES required")
def test_modaviva_academic_pdf_reaches_indicative_final_document() -> None:
    settings = _settings()
    diagnostic_only = os.getenv(DIAGNOSTIC_ONLY_ENV) == "YES"
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    api_image_id = _image_id(settings.api_image)
    worker_image_id = _image_id(settings.worker_image)
    token = uuid4().hex[:12]
    prefix = f"trackflow-hito9-academic-{token}"
    names = {key: f"{prefix}-{key}" for key in (
        "postgres", "redis", "stub", "api", "worker", "relay",
        "artifacts", "auth", "checkpoints",
    )}
    names["network"] = prefix
    containers: list[str] = []
    volumes: list[str] = []
    evidence_path = settings.evidence_dir / f"{prefix}-evidence.json"
    assert not evidence_path.exists()
    database_password = _generate_academic_secret("AcademicDb-")
    academic_password = _generate_academic_secret("AcademicUser-")
    jwt_secret = _generate_academic_secret("AcademicJwt-")
    database_url = _RedactedSecret(
        f"postgresql+psycopg2://trackflow_rfp_test:{database_password}"
        f"@{names['postgres']}:5432/"
        "trackflow_rfp_test"
    )
    common = {
        "DATABASE_URL": database_url,
        "REDIS_URL": f"redis://{names['redis']}:6379/0",
        "RFP_ARTIFACT_ROOT": "/app/data/raw/rfp_intake",
        "TRACKFLOW_ENVIRONMENT": "qa",
        "RFP_MAX_ATTEMPTS": "3",
        "RFP_LEASE_SECONDS": "600",
        "RFP_HEARTBEAT_SECONDS": "60",
        "RFP_MAX_PDF_PAGES": "100",
        "RFP_PDF_CONVERSION_TIMEOUT_SECONDS": "60",
    }
    try:
        _docker("network", "create", "--internal", names["network"], check=True)
        network = json.loads(
            _docker("network", "inspect", names["network"], check=True).stdout
        )[0]
        assert network["Internal"] is True
        for key in ("artifacts", "auth", "checkpoints"):
            _docker("volume", "create", names[key], check=True)
            volumes.append(names[key])
        _prepare_checkpoint_volume(
            volume=names["checkpoints"], image=settings.api_image,
        )

        _run_container(
            names["postgres"], names["network"], "postgres:16-alpine",
            environment={
                "POSTGRES_DB": "trackflow_rfp_test",
                "POSTGRES_USER": "trackflow_rfp_test",
                "POSTGRES_PASSWORD": database_password,
            },
            tmpfs=("/var/lib/postgresql/data:rw,noexec,nosuid,size=384m",),
        )
        containers.append(names["postgres"])
        _wait_until(
            lambda: _postgres_ready(names["postgres"]),
            timeout=30,
            description="isolated PostgreSQL",
        )
        assert _psql(
            names["postgres"],
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        ) == "0"
        for migration in MIGRATIONS:
            _apply_migration(
                container=names["postgres"], migration=migration,
                evidence_dir=settings.evidence_dir, evidence_prefix=prefix,
            )

        _run_container(
            names["redis"], names["network"], "redis:7.4.1-alpine",
            environment={},
            tmpfs=("/data:rw,noexec,nosuid,size=64m",),
            command=("redis-server", "--save", "", "--appendonly", "no"),
        )
        containers.append(names["redis"])
        _wait_until(
            lambda: "PONG" in _docker(
                "exec", names["redis"], "redis-cli", "ping"
            ).stdout,
            timeout=20,
            description="isolated Redis",
        )

        _run_container(
            names["stub"], names["network"], settings.worker_image,
            environment={}, command=("python", "-c", ACADEMIC_STUB_CODE),
        )
        containers.append(names["stub"])
        _wait_until(
            lambda: _docker(
                "exec", names["stub"], "python", "-c",
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080',timeout=1).read()",
            ).returncode == 0,
            timeout=20,
            description="controlled local stub",
        )

        _run_identity_seed(
            network=names["network"], auth_volume=names["auth"],
            image=settings.api_image, database_url=database_url,
            academic_password=academic_password, jwt_secret=jwt_secret,
            evidence_dir=settings.evidence_dir, evidence_prefix=prefix,
        )
        api_env = {
            **common,
            "TRACKFLOW_AUTH_DATABASE_PATH": "/app/services/api/data/auth.json",
            "JWT_SECRET_KEY": jwt_secret,
            "ACADEMIC_E2E_PASSWORD": academic_password,
            DIAGNOSTIC_ONLY_ENV: "YES" if diagnostic_only else "NO",
            "TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR": "/app/runtime",
            "TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH": "/app/runtime/approval.sqlite3",
            "LANGGRAPH_STRICT_MSGPACK": "true",
        }
        _run_container(
            names["api"], names["network"], settings.api_image,
            environment=api_env,
            volumes=(
                f"{names['artifacts']}:/app/data/raw/rfp_intake",
                f"{names['auth']}:/app/services/api/data",
                f"{names['checkpoints']}:/app/runtime",
                f"{FIXTURES.resolve()}:/e2e-fixtures:ro",
            ),
            command=("uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"),
        )
        containers.append(names["api"])
        _wait_until(
            lambda: _docker(
                "exec", names["api"], "python", "-c",
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1).read()",
            ).returncode == 0,
            timeout=40,
            description="isolated API",
        )

        worker_env = {
            **common,
            "FOUR_GEEKS_BASE_URL": f"http://{names['stub']}:8080",
            "FOUR_GEEKS_API_KEY": "controlled-local-stub-not-external",
            "QDRANT_URL": "http://unused.invalid",
            "RAG_REQUEST_INTERVAL_SECONDS": "0",
            "RAG_GENERATION_TIMEOUT_SECONDS": "10",
            "RFP_RESPONSE_LLM_MODE": "deterministic",
            "TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS": "240",
            "TRACKFLOW_TASK_TIME_LIMIT_SECONDS": "300",
            "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS": "900",
            "TRACKFLOW_RESULT_EXPIRES_SECONDS": "1200",
        }
        _run_container(
            names["worker"], names["network"], settings.worker_image,
            environment=worker_env,
            volumes=(f"{names['artifacts']}:/app/data/raw/rfp_intake",),
            command=(
                "celery", "--app", "services.task_queue.celery_app:celery_app",
                "worker", "--loglevel=INFO", "--concurrency=1",
                "--prefetch-multiplier=1", "--hostname=academic-e2e@%h",
            ),
        )
        containers.append(names["worker"])
        _wait_until(
                lambda: _celery_worker_ready(names["worker"]),
            timeout=40,
            description="isolated Celery worker",
        )
        _run_container(
            names["relay"], names["network"], settings.worker_image,
            environment={**common, "RFP_RELAY_POLL_SECONDS": "0.25"},
            command=("python", "-m", "services.rfp_intake.relay"),
        )
        containers.append(names["relay"])

        client_output = _run_academic_client(
            container=names["api"], evidence_dir=settings.evidence_dir,
            evidence_prefix=prefix,
        )
        journey = json.loads(client_output)
        if diagnostic_only:
            http_diagnostics_path = (
                settings.evidence_dir
                / f"{prefix}-initial-response.sanitized.json"
            )
            http_diagnostics_path.write_text(
                json.dumps(
                    {
                        "contract": (
                            "trackflow-hito9-academic-initial-response-v1"
                        ),
                        "part1_provider": journey["part1_provider"],
                        "ticket_id": journey["ticket_id"],
                        "response_run_id": journey["response_run_id"],
                        "approval_run_id": journey["approval_run_id"],
                        "initial_revisions": journey["initial_revisions"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            diagnostics_file = _capture_initial_revision_diagnostics(
                container=names["postgres"],
                evidence_dir=settings.evidence_dir,
                evidence_prefix=prefix,
            )
            diagnostics = json.loads(
                (settings.evidence_dir / diagnostics_file).read_text(
                    encoding="utf-8"
                )
            )
            assert {item["department_id"] for item in diagnostics} == {
                "warehouse", "reverse",
            }
            assert {
                item["department_id"]: item["revision_id"]
                for item in diagnostics
            } == {
                department: item["revision_id"]
                for department, item in journey["initial_revisions"].items()
            }
            evidence_path.write_text(
                json.dumps(
                    {
                        "contract": "trackflow-hito9-academic-initial-state-v1",
                        "outcome": "diagnostic_passed",
                        "part1_llm_boundary": "controlled_local_stub",
                        "part2_generation": "deterministic",
                        "official_pdf_sha256": FIXTURE_SHA256,
                        "api_image_id": api_image_id,
                        "worker_image_id": worker_image_id,
                        "network_internal": True,
                        "journey": journey,
                        "initial_revision_response_file": (
                            http_diagnostics_path.name
                        ),
                        "initial_revision_diagnostics_file": diagnostics_file,
                        "migration_sha256": {
                            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in MIGRATIONS
                        },
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return
        artifacts = json.loads(
            _docker(
                "run", "--rm", "--volume", f"{names['artifacts']}:/data:ro",
                settings.worker_image, "python", "-c",
                "import hashlib,json,pathlib;root=pathlib.Path('/data');"
                "pdfs=list(root.glob('*/source.pdf'));mds=list(root.glob('*/source.md'));"
                "assert len(pdfs)==len(mds)==1 and mds[0].stat().st_size>0;"
                "print(json.dumps({'pdf_sha256':hashlib.sha256(pdfs[0].read_bytes()).hexdigest(),"
                "'markdown_sha256':hashlib.sha256(mds[0].read_bytes()).hexdigest(),"
                "'markdown_bytes':mds[0].stat().st_size}))",
                check=True,
            ).stdout.strip()
        )
        assert artifacts["pdf_sha256"] == FIXTURE_SHA256
        checkpoint = json.loads(
            _docker(
                "run", "--rm", "--volume", f"{names['checkpoints']}:/runtime:ro",
                settings.worker_image, "python", "-c",
                "import hashlib,json,pathlib;p=pathlib.Path('/runtime/approval.sqlite3');"
                "assert p.is_file() and p.stat().st_size>0;"
                "print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
                "'bytes':p.stat().st_size}))",
                check=True,
            ).stdout.strip()
        )
        snapshot = json.loads(_psql(names["postgres"], """
          SELECT json_build_object(
            'tickets',(SELECT count(*) FROM rfp_tickets),
            'jobs',(SELECT count(*) FROM rfp_jobs),
            'outbox',(SELECT count(*) FROM rfp_outbox),
            'published_outbox',(SELECT count(*) FROM rfp_outbox WHERE status='published'),
            'metadata',(SELECT count(*) FROM rfp_metadata),
            'part1_sections',(SELECT count(*) FROM rfp_department_sections),
            'part1_handoffs',(SELECT count(*) FROM rfp_handoffs),
            'response_runs',(SELECT count(*) FROM rfp_response_runs),
            'response_drafts',(SELECT count(*) FROM rfp_response_drafts),
            'response_evaluations',(SELECT count(*) FROM rfp_response_evaluations),
            'response_handoffs',(SELECT count(*) FROM rfp_response_handoffs WHERE outcome='needs_human_review'),
            'authorized_inputs',(SELECT count(*) FROM rfp_authorized_business_inputs),
            'approval_runs',(SELECT count(*) FROM rfp_approval_runs WHERE status='completed'),
            'approved_revisions',(SELECT count(*) FROM rfp_approval_department_revisions WHERE status='approved'),
            'applied_decisions',(SELECT count(*) FROM rfp_human_decisions WHERE processing_status='applied'),
            'decision_events',(SELECT count(*) FROM rfp_workflow_events WHERE event_type='human_decision_applied'),
            'proposals',(SELECT count(*) FROM rfp_approval_proposal_revisions),
            'documents',(SELECT count(*) FROM rfp_final_documents)
          )
        """))
        assert snapshot["tickets"] == snapshot["metadata"] == 1
        assert snapshot["jobs"] == 2
        assert snapshot["outbox"] == snapshot["published_outbox"] >= 2
        assert snapshot["part1_sections"] == 2 and snapshot["part1_handoffs"] == 1
        assert snapshot["response_runs"] == snapshot["response_handoffs"] == 1
        assert snapshot["response_drafts"] == snapshot["response_evaluations"] == 3
        assert snapshot["authorized_inputs"] == 0
        assert snapshot["approval_runs"] == 1
        assert snapshot["approved_revisions"] == snapshot["applied_decisions"] == 2
        assert snapshot["decision_events"] == 2
        assert snapshot["proposals"] == snapshot["documents"] == 1

        evaluations = json.loads(_psql(names["postgres"], """
          SELECT json_object_agg(scope_key,json_build_object(
            'overall_pass',overall_pass,'terminal_status',terminal_status,
            'outcome',payload->>'outcome'
          )) FROM rfp_response_evaluations
        """))
        assert evaluations["warehouse"]["overall_pass"] is True
        assert evaluations["warehouse"]["terminal_status"] == "passed"
        assert evaluations["reverse"]["overall_pass"] is False
        assert evaluations["reverse"]["terminal_status"] == "needs_human_input"
        assert evaluations["proposal"]["overall_pass"] is False
        assert evaluations["proposal"]["outcome"] == "needs_human_review"
        response_handoff = json.loads(_psql(
            names["postgres"], "SELECT payload FROM rfp_response_handoffs"
        ))
        assert {
            item["code"] for item in response_handoff["unresolved_items"]
        } == {
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "RETURN_PROCESSING_RATE_REQUIRED",
        }

        final_row = json.loads(_psql(names["postgres"], """
          SELECT json_build_object(
            'payload',payload,'payload_hash',payload_hash,'markdown',markdown,
            'markdown_hash',markdown_hash,'proposal_revision_id',proposal_revision_id,
            'warehouse_revision_id',warehouse_revision_id,'reverse_revision_id',reverse_revision_id
          ) FROM rfp_final_documents
        """))
        assert final_row["payload_hash"] == canonical_hash(final_row["payload"])
        assert final_row["markdown_hash"] == hashlib.sha256(
            final_row["markdown"].encode("utf-8")
        ).hexdigest()
        manifest = final_row["payload"]["manifest"]
        assert manifest["department_revision_ids"] == [
            journey["revision_ids"]["warehouse"],
            journey["revision_ids"]["reverse"],
        ]
        assert manifest["authorized_input_ids"] == []
        decisions = json.loads(_psql(names["postgres"], """
          SELECT json_agg(json_build_object(
            'department',department_id,'decision_id',decision_id
          ) ORDER BY department_id) FROM rfp_human_decisions
        """))
        assert {item["decision_id"] for item in decisions} == set(
            manifest["approval_decision_ids"]
        )
        assert final_row["warehouse_revision_id"] == journey["revision_ids"]["warehouse"]
        assert final_row["reverse_revision_id"] == journey["revision_ids"]["reverse"]

        evidence = {
            "contract": "trackflow-hito9-academic-e2e-v1",
            "outcome": "passed",
            "part1_llm_boundary": "controlled_local_stub",
            "part1_stub_sha256": hashlib.sha256(
                ACADEMIC_STUB_CODE.encode("utf-8")
            ).hexdigest(),
            "part2_generation": "deterministic",
            "part3_decisions": "authenticated_http_and_langgraph_resume",
            "official_pdf_sha256": FIXTURE_SHA256,
            "api_image_id": api_image_id,
            "worker_image_id": worker_image_id,
            "network_internal": True,
            "migration_sha256": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in MIGRATIONS
            },
            "journey": journey,
            "artifacts": artifacts,
            "checkpoint": checkpoint,
            "response_evaluations": evaluations,
            "response_handoff_unresolved_items": sorted(
                item["code"] for item in response_handoff["unresolved_items"]
            ),
            "persistence_counts": snapshot,
            "final_document": {
                "payload_hash": final_row["payload_hash"],
                "markdown_hash": final_row["markdown_hash"],
                "proposal_revision_id": final_row["proposal_revision_id"],
            },
        }
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
        )
        assert evidence_path.is_file() and evidence_path.stat().st_size > 0
    except Exception as error:
        revision_diagnostics_file = None
        if names["postgres"] in containers:
            try:
                revision_diagnostics_file = _capture_initial_revision_diagnostics(
                    container=names["postgres"],
                    evidence_dir=settings.evidence_dir,
                    evidence_prefix=prefix,
                )
            except Exception:
                revision_diagnostics_file = "initial_revision_diagnostic_capture_failed"
        runtime_logs_file = None
        if containers:
            try:
                runtime_logs = _container_logs(containers)
                raw_runtime_path = (
                    settings.evidence_dir / f"{prefix}-runtime.logs.raw.txt"
                )
                sanitized_runtime_path = (
                    settings.evidence_dir / f"{prefix}-runtime.logs.sanitized.txt"
                )
                raw_runtime_path.write_text(runtime_logs, encoding="utf-8")
                sanitized_runtime_path.write_text(
                    _sanitize_psql_stderr(runtime_logs.encode("utf-8")),
                    encoding="utf-8",
                )
                runtime_logs_file = raw_runtime_path.name
            except Exception:
                runtime_logs_file = "runtime_log_capture_failed"
        if not evidence_path.exists():
            migration_failure = (
                {
                    "migration": error.migration,
                    "exit_code": error.exit_code,
                    "sqlstate": error.sqlstate,
                    "first_cause": error.first_cause,
                    "raw_stderr_file": error.raw_stderr_path.name,
                }
                if isinstance(error, AcademicE2EMigrationError)
                else None
            )
            bootstrap_failure = (
                {
                    "exit_code": error.exit_code,
                    "first_cause": error.first_cause,
                    "raw_stderr_file": error.raw_stderr_path.name,
                }
                if isinstance(error, AcademicE2EBootstrapError)
                else None
            )
            client_failure = (
                {
                    "exit_code": error.exit_code,
                    "first_cause": error.first_cause,
                    "raw_stderr_file": error.raw_stderr_path.name,
                }
                if isinstance(error, AcademicE2EClientError)
                else None
            )
            evidence_path.write_text(
                json.dumps(
                    {
                        "contract": "trackflow-hito9-academic-e2e-v1",
                        "outcome": "failed",
                        "error_type": type(error).__name__,
                        "part1_llm_boundary": "controlled_local_stub",
                        "part1_stub_sha256": hashlib.sha256(
                            ACADEMIC_STUB_CODE.encode("utf-8")
                        ).hexdigest(),
                        "official_pdf_sha256": FIXTURE_SHA256,
                        "api_image_id": api_image_id,
                        "worker_image_id": worker_image_id,
                        "network_internal": True,
                        "migration_failure": migration_failure,
                        "bootstrap_failure": bootstrap_failure,
                        "client_failure": client_failure,
                        "initial_revision_diagnostics_file": (
                            revision_diagnostics_file
                        ),
                        "runtime_logs_file": runtime_logs_file,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        raise
    finally:
        for container in reversed(containers):
            _docker("rm", "-f", container)
        for volume in reversed(volumes):
            _docker("volume", "rm", "-f", volume)
        _docker("network", "rm", names["network"])
