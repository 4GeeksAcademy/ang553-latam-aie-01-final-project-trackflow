"""Explicit response-contract coverage for the serialization audit phase."""

from __future__ import annotations

from fastapi.routing import APIRoute

from scripts.incidents.analyzer import analyze_records
from services.api.auth_models import (
    AuthMeResponse,
    ProfileMeResponse,
    RegistrationResponse,
    TokenResponse,
)
from services.api.main import HealthResponse, IncidentAnalysisResponse, app


def _route(method: str, path: str) -> APIRoute:
    def routes() -> list[APIRoute]:
        result: list[APIRoute] = []
        for registered in app.routes:
            if isinstance(registered, APIRoute):
                result.append(registered)
            elif hasattr(registered, "original_router"):
                result.extend(
                    route
                    for route in registered.original_router.routes
                    if isinstance(route, APIRoute)
                )
        return result

    matches = [route for route in routes() if route.path == path and method in route.methods]
    assert len(matches) == 1
    return matches[0]


def test_login_response_contract_has_only_token_fields() -> None:
    assert set(TokenResponse.model_fields) == {"access_token", "token_type"}
    assert TokenResponse(access_token="jwt", token_type="bearer").model_dump() == {
        "access_token": "jwt",
        "token_type": "bearer",
    }
    assert _route("POST", "/auth/login").response_model is TokenResponse


def test_auth_registration_and_profile_contracts_are_exact() -> None:
    assert set(AuthMeResponse.model_fields) == {"id", "email", "is_active", "role"}
    assert set(RegistrationResponse.model_fields) == {"message"}
    assert set(ProfileMeResponse.model_fields) == {"name", "phone", "address"}
    assert _route("GET", "/auth/me").response_model is AuthMeResponse
    assert _route("POST", "/users").response_model is RegistrationResponse
    assert _route("GET", "/profiles/me").response_model is ProfileMeResponse
    assert _route("PUT", "/profiles/me").response_model is ProfileMeResponse


def test_health_response_contract_preserves_stable_shape() -> None:
    response = HealthResponse(status="ok")

    assert set(HealthResponse.model_fields) == {"status"}
    assert response.model_dump() == {"status": "ok"}
    assert _route("GET", "/health").response_model is HealthResponse


def test_incident_analysis_result_validates_all_ten_fields() -> None:
    records = [
        {
            "incident_id": "INC-0001",
            "date": "2024-01-15",
            "country": "CO",
            "customer_type": "INDIVIDUAL",
            "tracking_number": "TRACK0001",
            "carrier": "Servientrega",
            "category": "DELIVERY_DELAY",
            "description": "Delivery arrived later than expected.",
            "status": "CLOSED",
            "customer_email": "customer@example.com",
            "satisfaction_score": "5",
        }
    ]
    result = IncidentAnalysisResponse.model_validate(analyze_records(records))

    assert set(result.model_fields_set) == {
        "total_records",
        "valid_records",
        "invalid_records",
        "invalid_breakdown",
        "category_breakdown",
        "status_breakdown",
        "country_breakdown",
        "closed_scored",
        "score_distribution",
        "average_satisfaction",
    }
    assert all(isinstance(value, int) for value in result.score_distribution.values())
    assert _route("POST", "/api/incidents/analyze").response_model is IncidentAnalysisResponse


def test_openapi_uses_nominal_response_schemas() -> None:
    openapi = app.openapi()

    assert (
        openapi["paths"]["/auth/login"]["post"]["responses"]["200"]["content"]
        ["application/json"]["schema"]["$ref"]
        == "#/components/schemas/TokenResponse"
    )
    assert (
        openapi["paths"]["/health"]["get"]["responses"]["200"]["content"]
        ["application/json"]["schema"]["$ref"]
        == "#/components/schemas/HealthResponse"
    )
    assert (
        openapi["paths"]["/api/incidents/analyze"]["post"]["responses"]["200"]
        ["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/IncidentAnalysisResponse"
    )