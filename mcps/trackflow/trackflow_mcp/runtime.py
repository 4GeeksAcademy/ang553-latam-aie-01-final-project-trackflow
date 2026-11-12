"""Environment-driven ASGI factory used by uvicorn deployments."""

from starlette.applications import Starlette

from .app import create_app
from .settings import MCPSettings


def create_runtime_app() -> Starlette:
    return create_app(MCPSettings.from_env())
