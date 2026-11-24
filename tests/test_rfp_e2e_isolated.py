"""Opt-in isolated RFP E2E across HTTP, outbox, Celery, graph and PostgreSQL.

The test owns every Docker resource it creates, never reads a production URL,
and uses an internal-only network plus a deterministic local generation stub.
Set TRACKFLOW_RFP_E2E=1 and provide the two freshly built image names to run it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import pytest


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "rfp_intake"
RUN_ENV = "TRACKFLOW_RFP_E2E"
API_IMAGE_ENV = "TRACKFLOW_RFP_E2E_API_IMAGE"
WORKER_IMAGE_ENV = "TRACKFLOW_RFP_E2E_WORKER_IMAGE"

pytestmark = pytest.mark.skipif(
    os.getenv(RUN_ENV) != "1"
    or not os.getenv(API_IMAGE_ENV)
    or not os.getenv(WORKER_IMAGE_ENV),
    reason=f"{RUN_ENV}=1, {API_IMAGE_ENV} and {WORKER_IMAGE_ENV} are required",
)


def _docker(*arguments: str, check: bool = False, input_text: str | None = None,
            timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], input=input_text, capture_output=True, text=True,
        check=check, timeout=timeout,
    )


def _wait_until(probe, *, timeout: float, description: str) -> object:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except Exception as exc:  # readiness probes are deliberately retried
            last_error = exc
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}: {last_error}")


def _run_container(name: str, network: str, image: str, *, environment: dict[str, str],
                   volumes: tuple[str, ...] = (), ports: tuple[str, ...] = (),
                   command: tuple[str, ...] = (), tmpfs: tuple[str, ...] = ()) -> None:
    args = ["run", "-d", "--name", name, "--network", network]
    for key, value in sorted(environment.items()):
        args.extend(["--env", f"{key}={value}"])
    for volume in volumes:
        args.extend(["--volume", volume])
    for port in ports:
        args.extend(["--publish", port])
    for mount in tmpfs:
        args.extend(["--tmpfs", mount])
    args.append(image)
    args.extend(command)
    _docker(*args, check=True)


def _psql(container: str, sql: str) -> str:
    result = _docker(
        "exec", container, "psql", "-v", "ON_ERROR_STOP=1", "-U",
        "trackflow_rfp_test", "-d", "trackflow_rfp_test", "-At", "-c", sql,
        check=True,
    )
    return result.stdout.strip()


def _container_logs(containers: list[str]) -> str:
    chunks = []
    for container in containers:
        result = _docker("logs", "--tail", "120", container)
        chunks.append(f"\n--- {container} ---\n{result.stdout}{result.stderr}")
    return "".join(chunks)


def _postgres_ready(container: str) -> bool:
    logs = _docker("logs", container).stdout
    if "PostgreSQL init process complete; ready for start up." not in logs:
        return False
    command = (
        "exec", container, "psql", "-U", "trackflow_rfp_test",
        "-d", "trackflow_rfp_test", "-At", "-c", "SELECT 1",
    )
    if _docker(*command).returncode != 0:
        return False
    time.sleep(0.75)
    return _docker(*command).returncode == 0


STUB_CODE = r'''
import json,re
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

failed_modaviva=False
def ref(n): return {"line_start":n,"line_end":n}
def find(lines,*needles):
 for index,line in enumerate(lines,1):
  if any(needle.casefold() in line.casefold() for needle in needles): return index
 raise ValueError("fixture evidence line not found")
def first_content_line(lines):
 for index,line in enumerate(lines,1):
  if line.strip(): return index
 return 1
def content_for(name,user):
 global failed_modaviva
 if name=="trackflow_rfp_classification":
  lines=user.split("<untrusted_rfp_markdown>\n",1)[1].rsplit("\n</untrusted_rfp_markdown>",1)[0].splitlines()
  if "ModaViva" in user and not failed_modaviva:
   failed_modaviva=True
   return "{invalid-json"
  if "RapidShip Carriers" in user:
   return json.dumps({"is_valid_rfp":False,"reason_code":"vendor_pitch",
    "evidence_refs":[ref(find(lines,"RapidShip Carriers"))]})
  if "ModaViva" in user or "Luna Cosmetics" in user:
   return json.dumps({"is_valid_rfp":True,"reason_code":"legitimate_rfp","evidence_refs":[ref(1)]})
  return json.dumps({"is_valid_rfp":False,"reason_code":"not_a_request",
   "evidence_refs":[ref(first_content_line(lines))]})
 if name=="trackflow_rfp_metadata":
  marker="<untrusted_rfp_markdown>\n"
  markdown=user.split(marker,1)[1].rsplit("\n</untrusted_rfp_markdown>",1)[0]
  lines=markdown.splitlines()
  if "ModaViva" in markdown:
   warehouse=find(lines,"Almacenamiento de aproximadamente")
   reverse=find(lines,"Gesti","devoluciones")
   excluded=find(lines,"No se requiere")
   deadline=find(lines,"Proposal Due Date")
   return json.dumps({"rfp_id":"MV-LOG-2026-03","client_name":"ModaViva, S.L.",
    "client_country_code":"ES","client_country_original":"España","operation_country_codes":["ES"],
    "services_requested":[
     {"service_id":"warehouse","requested":True,"source_ref":ref(warehouse)},
     {"service_id":"lastmile","requested":False,"source_ref":ref(excluded)},
     {"service_id":"reverse","requested":True,"source_ref":ref(reverse)}],
    "monthly_volume_value":None,"monthly_volume_unit":None,"monthly_volume_source_ref":None,
    "deadline":"2026-08-28","deadline_source_ref":ref(deadline),"onboarding_target":None,
    "onboarding_source_ref":None,"budget_range":None,"open_questions":["Monthly order volume is not provided."]})
  if "Luna Cosmetics" in markdown:
   services=find(lines,"warehousing and shipping")
   volume=find(lines,"5,000 orders a month")
   onboarding_start=find(lines,"up and running")
   onboarding_end=find(lines,"around August 24th")
   return json.dumps({"rfp_id":None,"client_name":"Luna Cosmetics",
    "client_country_code":"US","client_country_original":"United States","operation_country_codes":["US"],
    "services_requested":[
     {"service_id":"warehouse","requested":True,"source_ref":ref(services)},
     {"service_id":"lastmile","requested":True,"source_ref":ref(services)}],
    "monthly_volume_value":5000,"monthly_volume_unit":"orders/month","monthly_volume_source_ref":ref(volume),
    "deadline":None,"deadline_source_ref":None,
    "onboarding_target":"within the next 3 weeks (around August 24th)",
    "onboarding_source_ref":{"line_start":onboarding_start,"line_end":onboarding_end},
    "budget_range":None,"open_questions":[]})
  raise ValueError("unknown controlled fixture")
 if name.startswith("trackflow_rfp_") and name.endswith("_section"):
  department=name[len("trackflow_rfp_"):-len("_section")]
  match=re.search(r"(?m)^L(\d+):",user)
  line=int(match.group(1)) if match else 1
  return json.dumps({"department_id":department,"key_aspects":[{"fact":"Controlled local E2E fact",
   "source_ref":ref(line),"requested_action":"Confirm operational response"}],
   "open_questions":[],"source_references":[ref(line)],"detected_conflicts":[]})
 if name=="trackflow_rfp_synthesis":
  return json.dumps({"summary_for_sales":"Controlled local E2E summary.","open_questions":[],"detected_conflicts":[]})
 raise ValueError(name)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
 def do_POST(self):
  try:
   length=int(self.headers.get("Content-Length","0")); body=json.loads(self.rfile.read(length))
   name=body["response_format"]["json_schema"]["name"]
   user=body["messages"][-1]["content"]
   payload={"choices":[{"message":{"content":content_for(name,user)}}],"usage":{"total_tokens":1}}
   raw=json.dumps(payload).encode(); self.send_response(200)
   self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw)))
   self.end_headers(); self.wfile.write(raw)
  except Exception as exc:
   raw=json.dumps({"error":"controlled_stub_error"}).encode(); self.send_response(500)
   self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
 def log_message(self,*args): pass
ThreadingHTTPServer(("0.0.0.0",8080),Handler).serve_forever()
'''

CLIENT_CODE = r'''
import json,pathlib,time
import httpx
with httpx.Client(base_url="http://127.0.0.1:8000",timeout=20) as client:
 login=client.post("/auth/login",data={"username":"rfp-e2e@trackflow.test","password":"RfpE2ePass123!"})
 login.raise_for_status(); headers={"Authorization":"Bearer "+login.json()["access_token"]}
 tickets={}
 for fixture in sorted(pathlib.Path("/e2e-fixtures").glob("*.pdf")):
  response=client.post("/rfps",headers=headers,files={"file":(fixture.name,fixture.read_bytes(),"application/pdf")})
  response.raise_for_status(); assert response.status_code==202
  tickets[fixture.name]=response.json()["ticket_id"]
 deadline=time.monotonic()+120; details={}
 while time.monotonic()<deadline:
  details={name:client.get(f"/rfps/{ticket_id}",headers=headers).json() for name,ticket_id in tickets.items()}
  if all(item.get("status") in {"analisis_completo","descartado"} for item in details.values()): break
  time.sleep(1)
 else: raise RuntimeError("terminal RFP polling timed out")
 print(json.dumps({"tickets":tickets,"details":details},ensure_ascii=True))
'''


def test_rfp_http_outbox_celery_graph_postgres_e2e() -> None:
    api_image = os.environ[API_IMAGE_ENV]
    worker_image = os.environ[WORKER_IMAGE_ENV]
    token = uuid4().hex[:12]
    prefix = f"trackflow-rfp-e2e-{token}"
    names = {
        "network": prefix,
        "postgres": f"{prefix}-postgres",
        "redis": f"{prefix}-redis",
        "stub": f"{prefix}-stub",
        "api": f"{prefix}-api",
        "worker": f"{prefix}-worker",
        "relay": f"{prefix}-relay",
        "artifacts": f"{prefix}-artifacts",
        "auth": f"{prefix}-auth",
    }
    containers: list[str] = []
    volumes: list[str] = []
    api_url = "internal://api:8000"
    try:
        for image in (api_image, worker_image):
            inspection = _docker("image", "inspect", image, check=True).stdout
            assert json.loads(inspection)[0]["Config"]["User"] == "10002:10002"

        _docker("network", "create", "--internal", names["network"], check=True)
        network_inspection = json.loads(_docker("network", "inspect", names["network"], check=True).stdout)[0]
        assert network_inspection["Internal"] is True
        for key in ("artifacts", "auth"):
            _docker("volume", "create", names[key], check=True)
            volumes.append(names[key])

        _run_container(
            names["postgres"], names["network"], "postgres:16-alpine",
            environment={"POSTGRES_DB":"trackflow_rfp_test","POSTGRES_USER":"trackflow_rfp_test",
                         "POSTGRES_PASSWORD":"rfp-e2e-only"},
            tmpfs=("/var/lib/postgresql/data:rw,noexec,nosuid,size=256m",),
        )
        containers.append(names["postgres"])
        _wait_until(
            lambda: _postgres_ready(names["postgres"]),
            timeout=30, description="PostgreSQL",
        )
        assert _psql(names["postgres"], "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'") == "0"
        for migration in ("rfp_intake.sql", "rfp_sse.sql"):
            ddl = (ROOT / "infra" / "sql" / migration).read_text(encoding="utf-8")
            _docker(
                "exec", "-i", names["postgres"], "psql", "-v", "ON_ERROR_STOP=1", "-U",
                "trackflow_rfp_test", "-d", "trackflow_rfp_test", check=True, input_text=ddl,
            )

        _run_container(
            names["redis"], names["network"], "redis:7.4.1-alpine",
            environment={}, tmpfs=("/data:rw,noexec,nosuid,size=64m",),
            command=("redis-server", "--save", "", "--appendonly", "no"),
        )
        containers.append(names["redis"])
        _wait_until(
            lambda: "PONG" in _docker("exec", names["redis"], "redis-cli", "ping").stdout,
            timeout=20, description="Redis",
        )

        _run_container(
            names["stub"], names["network"], worker_image, environment={},
            command=("python", "-c", STUB_CODE),
        )
        containers.append(names["stub"])
        _wait_until(
            lambda: _docker("exec", names["stub"], "python", "-c",
                            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080',timeout=1).read()").returncode == 0,
            timeout=20, description="local generation stub",
        )

        database_url = "postgresql://trackflow_rfp_test:rfp-e2e-only@" + names["postgres"] + ":5432/trackflow_rfp_test"
        common = {
            "DATABASE_URL": database_url,
            "REDIS_URL": f"redis://{names['redis']}:6379/0",
            "RFP_ARTIFACT_ROOT": "/app/data/raw/rfp_intake",
            "RFP_MAX_ATTEMPTS": "3", "RFP_LEASE_SECONDS": "600", "RFP_HEARTBEAT_SECONDS": "60",
            "RFP_MAX_PDF_PAGES": "100", "RFP_PDF_CONVERSION_TIMEOUT_SECONDS": "60",
        }
        seed_code = (
            "from services.api.auth_database import users;"
            "from services.api.auth_models import UserInDB,Role;"
            "from services.api.auth_security import hash_password;"
            "users.insert(UserInDB(id='rfp-e2e-admin',email='rfp-e2e@trackflow.test',"
            "hashed_password=hash_password('RfpE2ePass123!'),role=Role.admin).model_dump(mode='json'))"
        )
        _docker(
            "run", "--rm", "--volume", f"{names['auth']}:/app/services/api/data",
            "--env", "TRACKFLOW_AUTH_DATABASE_PATH=/app/services/api/data/auth.json",
            "--env", "JWT_SECRET_KEY=rfp-e2e-secret-only-not-production",
            api_image, "python", "-c", seed_code, check=True,
        )

        api_env = {**common, "TRACKFLOW_AUTH_DATABASE_PATH":"/app/services/api/data/auth.json",
                   "JWT_SECRET_KEY":"rfp-e2e-secret-only-not-production"}
        _run_container(
            names["api"], names["network"], api_image, environment=api_env,
            volumes=(f"{names['artifacts']}:/app/data/raw/rfp_intake", f"{names['auth']}:/app/services/api/data",
                     f"{FIXTURES.resolve()}:/e2e-fixtures:ro"),
            command=("uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"),
        )
        containers.append(names["api"])
        _wait_until(
            lambda: _docker(
                "exec", names["api"], "python", "-c",
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1).read()",
            ).returncode == 0,
            timeout=40, description="TrackFlow API",
        )

        worker_env = {
            **common, "FOUR_GEEKS_BASE_URL":f"http://{names['stub']}:8080",
            "FOUR_GEEKS_API_KEY":"local-e2e-key", "QDRANT_URL":"http://unused.invalid",
            "RAG_REQUEST_INTERVAL_SECONDS":"0", "RAG_GENERATION_TIMEOUT_SECONDS":"10",
            "TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS":"240", "TRACKFLOW_TASK_TIME_LIMIT_SECONDS":"300",
            "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS":"900", "TRACKFLOW_RESULT_EXPIRES_SECONDS":"1200",
        }
        _run_container(
            names["worker"], names["network"], worker_image, environment=worker_env,
            volumes=(f"{names['artifacts']}:/app/data/raw/rfp_intake",),
            command=("celery", "--app", "services.task_queue.celery_app:celery_app", "worker",
                     "--loglevel=INFO", "--concurrency=3", "--hostname=rfp-e2e@%h"),
        )
        containers.append(names["worker"])
        _run_container(
            names["relay"], names["network"], worker_image,
            environment={**common, "RFP_RELAY_POLL_SECONDS":"0.25"},
            command=("python", "-m", "services.rfp_intake.relay"),
        )
        containers.append(names["relay"])

        client_result = _docker(
            "exec", names["api"], "python", "-c", CLIENT_CODE,
            check=True, timeout=150,
        )
        client_payload = json.loads(client_result.stdout.strip())
        ticket_by_fixture = client_payload["tickets"]
        details = client_payload["details"]
        assert len(ticket_by_fixture) == 3
        for detail in details.values():
            assert detail["job"]["status"] == "succeeded"
            assert "raw_pdf_path" not in json.dumps(detail)
            assert "markdown_path" not in json.dumps(detail)

        moda = details["trackflow-request-1.pdf"]
        assert moda["status"] == "analisis_completo"
        assert moda["rfp_id"] == "MV-LOG-2026-03"
        assert moda["metadata"]["client_name"] == "ModaViva, S.L."
        assert moda["metadata"]["client_country_code"] == "ES"
        assert moda["metadata"]["currency"] == "EUR"
        assert moda["metadata"]["deadline"] == "2026-08-28"
        assert [(item["service_id"], item["requested"]) for item in moda["metadata"]["services_requested"]] == [
            ("warehouse", True), ("lastmile", False), ("reverse", True)
        ]
        assert {(item["department_id"], item["responsible"]) for item in moda["department_sections"]} == {
            ("warehouse", "Ana Whitfield"), ("reverse", "Sofía Ramos")
        }
        assert {(item["department_id"], item["responsible"]) for item in moda["handoff"]["department_section_references"]} == {
            ("warehouse", "Ana Whitfield"), ("reverse", "Sofía Ramos")
        }

        luna = details["trackflow-request-2.pdf"]
        assert luna["status"] == "analisis_completo"
        assert luna["metadata"]["client_name"] == "Luna Cosmetics"
        assert luna["metadata"]["client_country_code"] == "US"
        assert luna["metadata"]["currency"] == "USD"
        assert luna["metadata"]["monthly_volume_value"] == 5000
        assert luna["metadata"]["monthly_volume_unit"] == "orders/month"
        assert luna["metadata"]["deadline"] is None
        assert "August 24th" in luna["metadata"]["onboarding_target"]
        assert {(item["department_id"], item["responsible"]) for item in luna["department_sections"]} == {
            ("warehouse", "Ana Whitfield"), ("lastmile", "Carlos Vega")
        }
        assert {(item["department_id"], item["responsible"]) for item in luna["handoff"]["department_section_references"]} == {
            ("warehouse", "Ana Whitfield"), ("lastmile", "Carlos Vega")
        }

        rapidship = details["trackflow-request-3.pdf"]
        assert rapidship["status"] == "descartado"
        assert rapidship["discard_reason_code"] == "vendor_pitch"
        assert rapidship["metadata"]["services_requested"] == []
        assert rapidship["department_sections"] == []
        assert rapidship["handoff"] is None

        snapshot_sql = """
        SELECT json_build_object(
          'tickets',(SELECT count(*) FROM rfp_tickets),
          'jobs',(SELECT count(*) FROM rfp_jobs),
          'attempts',(SELECT sum(attempt_count) FROM rfp_jobs),
          'succeeded',(SELECT count(*) FROM rfp_jobs WHERE status='succeeded'),
          'complete',(SELECT count(*) FROM rfp_tickets WHERE status='analisis_completo'),
          'discarded',(SELECT count(*) FROM rfp_tickets WHERE status='descartado'),
          'metadata',(SELECT count(*) FROM rfp_metadata),
          'handoffs',(SELECT count(*) FROM rfp_handoffs),
          'sections',(SELECT count(*) FROM rfp_department_sections),
          'outbox',(SELECT count(*) FROM rfp_outbox),
          'published',(SELECT count(*) FROM rfp_outbox WHERE status='published'))
        """
        before = json.loads(_psql(names["postgres"], snapshot_sql))
        assert before["tickets"] == before["jobs"] == before["succeeded"] == 3
        assert before["complete"] == 2 and before["discarded"] == 1
        assert before["metadata"] == 3 and before["handoffs"] == 2
        assert before["sections"] == 4
        assert before["attempts"] == 4
        assert before["outbox"] == before["published"] == 4

        retry_evidence = json.loads(_psql(names["postgres"], """
          SELECT json_agg(row_to_json(x)) FROM (
            SELECT j.job_id,j.ticket_id,j.attempt_count,
              (SELECT count(*) FROM rfp_outbox o WHERE o.job_id=j.job_id) AS outbox_count
            FROM rfp_jobs j ORDER BY j.attempt_count DESC,j.job_id
          ) x
        """))
        assert [row["attempt_count"] for row in retry_evidence] == [2, 1, 1]
        assert retry_evidence[0]["outbox_count"] == 2
        assert str(retry_evidence[0]["ticket_id"]) == ticket_by_fixture["trackflow-request-1.pdf"]

        duplicate_job_id = next(row["job_id"] for row in retry_evidence if row["attempt_count"] == 1)
        publish_code = (
            "from services.task_queue.celery_app import celery_app;"
            f"celery_app.send_task('trackflow.process_rfp',kwargs={{'job_id':'{duplicate_job_id}'}})"
        )
        _docker(
            "run", "--rm", "--network", names["network"], "--env",
            f"REDIS_URL=redis://{names['redis']}:6379/0", worker_image,
            "python", "-c", publish_code, check=True,
        )
        time.sleep(4)
        assert json.loads(_psql(names["postgres"], snapshot_sql)) == before

        artifact_code = (
            "import json,os,pathlib;root=pathlib.Path('/data');"
            "rows=[{'ticket_id':p.name,'uid':p.stat().st_uid,'gid':p.stat().st_gid,"
            "'files':sorted(x.name for x in p.iterdir())} for p in root.iterdir() if p.is_dir()];"
            "print(json.dumps(sorted(rows,key=lambda x:x['ticket_id'])))"
        )
        artifact_result = _docker(
            "run", "--rm", "--volume", f"{names['artifacts']}:/data", worker_image,
            "python", "-c", artifact_code, check=True,
        )
        artifacts = json.loads(artifact_result.stdout)
        assert len(artifacts) == 3
        assert all(row["uid"] == 10002 and row["gid"] == 10002 for row in artifacts)
        assert all(row["files"] == ["source.md", "source.pdf"] for row in artifacts)
    except Exception as exc:
        logs = _container_logs(containers)
        raise AssertionError(f"RFP isolated E2E failed: {exc}\nAPI={api_url}{logs}") from exc
    finally:
        for container in reversed(containers):
            _docker("rm", "-f", container)
        for volume in reversed(volumes):
            _docker("volume", "rm", "-f", volume)
        _docker("network", "rm", names["network"])
