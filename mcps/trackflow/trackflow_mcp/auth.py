"""MCP Auth integration and operation-level authorization."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from mcpauth import (
    AuthInfo,
    AuthServerConfig,
    MCPAuth,
    ResourceServerConfig,
)
from mcpauth.config import AuthServerType, AuthorizationServerMetadata
from mcpauth.types import ResourceServerMetadata
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from .settings import MCPSettings, SUPPORTED_SCOPES

Lifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


@dataclass(frozen=True)
class ExternalIdentity:
    """Signed external identity; roles and tenant memberships are deliberately absent."""

    trackflow_user_id: str
    subject: str
    issuer: str
    client_id: str | None
    scopes: frozenset[str]


class MCPIdentityError(RuntimeError):
    status_code = 403


class MCPPermissionDenied(RuntimeError):
    status_code = 403

    def __init__(self, missing_scopes: tuple[str, ...]):
        super().__init__("Operation is not authorized.")
        self.missing_scopes = missing_scopes


@dataclass(frozen=True)
class AuthRuntime:
    mcp_auth: MCPAuth
    context: ContextVar[AuthInfo | None]


def build_auth_runtime(settings: MCPSettings) -> AuthRuntime:
    auth_server = AuthServerConfig(
        type=AuthServerType.OIDC,
        metadata=AuthorizationServerMetadata(
            issuer=settings.issuer,
            authorization_endpoint=settings.authorization_endpoint,
            token_endpoint=settings.token_endpoint,
            jwks_uri=settings.jwks_uri,
            response_types_supported=["code"],
            grant_types_supported=["authorization_code"],
            code_challenge_methods_supported=["S256"],
            scopes_supported=["openid", *SUPPORTED_SCOPES],
        ),
    )
    protected_resource = ResourceServerConfig(
        metadata=ResourceServerMetadata(
            resource=settings.resource_url,
            authorization_servers=[auth_server],
            scopes_supported=list(SUPPORTED_SCOPES),
            bearer_methods_supported=["header"],
            resource_name="TrackFlow MCP Server",
        )
    )
    context: ContextVar[AuthInfo | None] = ContextVar(
        "trackflow_mcp_auth_context", default=None
    )
    return AuthRuntime(
        mcp_auth=MCPAuth(protected_resources=protected_resource, context_var=context),
        context=context,
    )


def external_identity(runtime: AuthRuntime) -> ExternalIdentity:
    info = runtime.context.get()
    if info is None:
        raise MCPIdentityError("Authenticated identity is unavailable.")
    user_id = info.claims.get("trackflow_user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise MCPIdentityError("Authenticated identity is not linked to TrackFlow.")
    if not isinstance(info.subject, str) or not info.subject.strip():
        raise MCPIdentityError("Authenticated subject is unavailable.")
    return ExternalIdentity(
        trackflow_user_id=user_id,
        subject=info.subject,
        issuer=info.issuer,
        client_id=info.client_id,
        scopes=frozenset(info.scopes),
    )


def require_scopes(runtime: AuthRuntime, *required: str) -> ExternalIdentity:
    unsupported = tuple(scope for scope in required if scope not in SUPPORTED_SCOPES)
    if unsupported:
        raise ValueError("Unsupported TrackFlow MCP scope requested by server code.")
    identity = external_identity(runtime)
    missing = tuple(scope for scope in required if scope not in identity.scopes)
    if missing:
        raise MCPPermissionDenied(missing)
    return identity


class _ContextIsolationMiddleware:
    def __init__(self, app: ASGIApp, context: ContextVar[AuthInfo | None]):
        self.app = app
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        reset_token: Token[AuthInfo | None] = self.context.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            self.context.reset(reset_token)


class _IdentityBindingMiddleware:
    def __init__(self, app: ASGIApp, runtime: AuthRuntime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                identity = external_identity(self.runtime)
            except MCPIdentityError:
                response = JSONResponse(
                    {"error": "identity_not_linked"}, status_code=MCPIdentityError.status_code
                )
                await response(scope, receive, send)
                return
            scope["trackflow.identity"] = identity
        await self.app(scope, receive, send)


def protect_streamable_http_app(
    inner_app: ASGIApp,
    settings: MCPSettings,
    runtime: AuthRuntime,
    *,
    lifespan: Lifespan | None = None,
) -> Starlette:
    """Publish PRM publicly and protect every non-metadata request with MCP Auth."""

    bearer_middleware = runtime.mcp_auth.bearer_auth_middleware(
        "jwt",
        audience=settings.audience,
        required_scopes=[],
        show_error_details=False,
        leeway=0,
        resource=settings.resource_url,
    )
    protected: ASGIApp = _IdentityBindingMiddleware(inner_app, runtime)
    protected = bearer_middleware(protected)
    protected = _ContextIsolationMiddleware(protected, runtime.context)

    metadata_routes = list(runtime.mcp_auth.resource_metadata_router().routes)
    return Starlette(
        routes=[*metadata_routes, Mount("/", app=protected)],
        lifespan=lifespan,
    )
