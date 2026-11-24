from pathlib import Path
import tomllib


ROOT = Path(__file__).parents[1]


def test_rfp_approval_service_is_included_in_python_package_discovery() -> None:
    configuration = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    includes = configuration["tool"]["setuptools"]["packages"]["find"]["include"]

    assert "services.rfp_approval*" in includes
    assert "pipelines*" in includes


def test_api_image_installs_the_persistent_sqlite_checkpointer() -> None:
    requirements = (
        ROOT / "services" / "api" / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()
    runtime = (
        ROOT / "services" / "rfp_approval" / "runtime.py"
    ).read_text(encoding="utf-8")

    assert "from langgraph.checkpoint.sqlite import SqliteSaver" in runtime
    assert "langgraph-checkpoint-sqlite==3.1.3" in requirements
