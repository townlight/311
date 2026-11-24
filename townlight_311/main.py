"""FastAPI runtime foundation for Townlight 311."""

import os
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from townlight_311 import __version__
from townlight_311.deduplication import check_duplicate_candidates
from townlight_311.open311_export import build_open311_export
from townlight_311.persistence import ServiceRequestRepository, StoredTriageReview
from townlight_311.public_ui import render_public_lookup_page
from townlight_311.request_intake import create_service_request_stub
from townlight_311.routing import plan_department_route
from townlight_311.triage import suggest_triage

TOWNLIGHT_CORE_VERSION = _pkg_version("townlight-core")


app = FastAPI(
    title='Townlight 311',
    version=__version__,
    description='Resident service request intake, triage, deduplication, routing, and Open311-compatible export support for Townlight.',
)

_request_repository: ServiceRequestRepository | None = None
_request_db_url: str | None = None


class IntakeRequest(BaseModel):
    request_id: str
    category: str
    description: str
    location: str


class TriageRequest(BaseModel):
    request_id: str = "unassigned-request"
    category: str
    description: str
    location: str


class DeduplicationRequest(BaseModel):
    request_id: str
    category: str
    location: str
    candidate_count: int = 0


class RoutingRequest(BaseModel):
    category: str
    department: str


class Open311ExportRequest(BaseModel):
    service_request_id: str
    service_code: str
    format: str = "json"


@app.get("/")
def root() -> dict[str, str]:
    """Return current product state without overstating unshipped behavior."""

    return {
        "name": "Townlight 311",
        "version": __version__,
        "status": "developer-only prototype helper API; 0.2.0 source work is an integration candidate and is not released or tested-installed",
        "message": (
            "This module is a developer-only prototype helper API. It is not the Windows runtime and is not a "
            "complete standalone product. The implemented Windows routes are /desktop/v1/service-requests, with "
            "Open311 at /desktop/v1/service-requests/open311/... . The townlight_311.workflow library is under "
            "integration verification; it is a candidate and is not installed."
        ),
        "next_step": (
            "Use the implemented Windows routes. The 0.2.0 source work is an integration candidate and has not "
            "been released or tested as an installed Windows build."
        ),
    }


@app.get("/health")
def health() -> dict[str, str]:
    """Return dependency/version health for deployment smoke checks."""

    return {
        "status": "ok",
        "service": "townlight_311",
        "version": __version__,
        "townlight_core_version": TOWNLIGHT_CORE_VERSION,
    }


@app.get("/townlight_311", response_class=HTMLResponse)
def public_townlight_311_page() -> str:
    """Return the public sample 311 support UI."""

    return render_public_lookup_page()


@app.post("/api/v1/townlight_311/intake")
def service_request_intake(request: IntakeRequest) -> dict[str, object]:
    result = _create_service_request(
        request_id=request.request_id,
        category=request.category,
        description=request.description,
        location=request.location,
    )
    return result.__dict__


@app.get("/api/v1/townlight_311/intake/{request_id}")
def get_service_request(request_id: str) -> dict[str, object]:
    if _request_database_url() is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Townlight 311 request persistence is not configured.",
                "fix": "Set CIVIC311_REQUEST_DB_URL to retrieve persisted service request records.",
            },
        )
    stored = _get_request_repository().get_request(request_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Service request record not found.",
                "fix": "Use a request_id returned by POST /api/v1/townlight_311/intake.",
            },
        )
    return stored.__dict__


@app.post("/api/v1/townlight_311/triage")
def triage(request: TriageRequest) -> dict[str, object]:
    if _request_database_url() is not None:
        stored = _get_request_repository().create_triage_review(
            request_id=request.request_id,
            category=request.category,
            description=request.description,
            location=request.location,
        )
        return _stored_triage_response(stored)

    return suggest_triage(
        category=request.category,
        description=request.description,
        location=request.location,
    ).__dict__


@app.get("/api/v1/townlight_311/triage/{request_id}")
def get_triage_review(request_id: str) -> dict[str, object]:
    if _request_database_url() is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Townlight 311 request persistence is not configured.",
                "fix": "Set CIVIC311_REQUEST_DB_URL to retrieve persisted triage review records.",
            },
        )
    stored = _get_request_repository().get_triage_review(request_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Triage review record not found.",
                "fix": "Use a request_id returned by POST /api/v1/townlight_311/triage.",
            },
        )
    return _stored_triage_response(stored)


@app.post("/api/v1/townlight_311/deduplicate")
def deduplicate(request: DeduplicationRequest) -> dict[str, object]:
    return check_duplicate_candidates(
        request_id=request.request_id,
        category=request.category,
        location=request.location,
        candidate_count=request.candidate_count,
    ).__dict__


@app.post("/api/v1/townlight_311/routing")
def routing(request: RoutingRequest) -> dict[str, object]:
    return plan_department_route(
        category=request.category,
        department=request.department,
    ).__dict__


@app.post("/api/v1/townlight_311/open311-export")
def open311_export(request: Open311ExportRequest) -> dict[str, object]:
    return build_open311_export(
        service_request_id=request.service_request_id,
        service_code=request.service_code,
        format=request.format,
    ).__dict__


def _request_database_url() -> str | None:
    return os.environ.get("CIVIC311_REQUEST_DB_URL")


def _get_request_repository() -> ServiceRequestRepository:
    global _request_db_url, _request_repository
    db_url = _request_database_url()
    if db_url is None:
        raise RuntimeError("CIVIC311_REQUEST_DB_URL is not configured.")
    if _request_repository is None or db_url != _request_db_url:
        _dispose_request_repository()
        _request_db_url = db_url
        _request_repository = ServiceRequestRepository(db_url=db_url)
    return _request_repository


def _dispose_request_repository() -> None:
    global _request_repository
    if _request_repository is not None:
        _request_repository.engine.dispose()
        _request_repository = None


def _create_service_request(*, request_id: str, category: str, description: str, location: str):
    if _request_database_url() is None:
        return create_service_request_stub(
            request_id=request_id,
            category=category,
            description=description,
            location=location,
        )
    return _get_request_repository().create_request(
        request_id=request_id,
        category=category,
        description=description,
        location=location,
    )


def _stored_triage_response(stored: StoredTriageReview) -> dict[str, object]:
    return {
        "request_id": stored.request_id,
        "category": stored.category,
        "priority": stored.priority,
        "suggested_department": stored.suggested_department,
        "reasons": list(stored.reasons),
        "staff_review_required": stored.staff_review_required,
        "disclaimer": stored.disclaimer,
        "created_at": stored.created_at.isoformat(),
    }
