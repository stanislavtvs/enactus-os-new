from datetime import datetime
from typing import Annotated, Literal

from django.http import HttpResponse
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import Field, StringConstraints, field_validator

from apps.accounts import throttle

from . import platform
from .models import ORGANIZATION_NAME_MAX_LENGTH, OrganizationCreationRequest, PlatformCreationPolicy

router = Router(auth=django_auth)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ORGANIZATION_NAME_MAX_LENGTH)]


class PolicyData(Schema):
    mode: PlatformCreationPolicy.Mode
    can_create: bool
    can_request: bool
    can_administer: bool
    email_verified: bool


class PolicyChange(Schema):
    mode: PlatformCreationPolicy.Mode


class UserData(Schema):
    id: int
    username: str


class RequestData(Schema):
    id: int
    applicant: UserData
    name: str
    description: str
    status: OrganizationCreationRequest.Status
    created_at: datetime
    reviewer: UserData | None
    decided_at: datetime | None
    decision_comment: str
    organization_id: int | None


class RequestInput(Schema):
    name: Name
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=platform.REQUEST_DESCRIPTION_MAX_LENGTH),
    ]


class DecisionInput(Schema):
    decision: Literal["approve", "reject"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=platform.DECISION_COMMENT_MAX_LENGTH)] = ""


class GrantInput(Schema):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        return _safe_query_text(value)


class GrantData(Schema):
    user: UserData
    granted_by: UserData
    granted_at: datetime
    revoked_at: datetime | None


def _safe_query_text(value):
    if not platform.valid_text(value):
        raise ValueError("Недопустимый символ.")
    return value


class SearchQuery(Schema):
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=150)] = ""

    @field_validator("q")
    @classmethod
    def validate_search(cls, value):
        return _safe_query_text(value)


class PageQuery(Schema):
    page: int = Field(1, ge=1, le=1_000_000)
    page_size: int = Field(25, ge=1, le=100)


class GrantQuery(PageQuery, SearchQuery):
    pass


class RequestQuery(PageQuery):
    status: OrganizationCreationRequest.Status | None = None


class AdminRequestQuery(RequestQuery, SearchQuery):
    pass


class PageData(Schema):
    total: int
    page: int
    page_size: int


class RequestPage(PageData):
    items: list[RequestData]


class GrantPage(PageData):
    items: list[GrantData]


class GrantCandidateData(Schema):
    items: list[UserData]


def _perform(response, operation, **kwargs):
    response["Cache-Control"] = "no-store"
    try:
        return operation(**kwargs)
    except platform.CreationRateLimited as exc:
        raise HttpError(429, str(exc)) from None
    except platform.CreationConflict as exc:
        raise HttpError(409, str(exc)) from None
    except platform.CreationValidation as exc:
        raise HttpError(422, str(exc)) from None


def _page(rows, query):
    offset = (query.page - 1) * query.page_size
    return {
        "items": list(rows[offset:offset + query.page_size]), "total": rows.count(),
        "page": query.page, "page_size": query.page_size,
    }


@router.get("/creation-policy/", response=PolicyData, url_name="creation_policy")
def get_policy(request, response: HttpResponse):
    return _perform(response, platform.creation_policy, user=request.user)


@router.patch("/creation-policy/", response=PolicyData)
def set_policy(request, response: HttpResponse, payload: PolicyChange):
    return _perform(response, platform.set_creation_policy, user=request.user, mode=payload.mode)


@router.get("/creation-requests/", response=RequestPage, url_name="platform_creation_requests")
def own_requests(request, response: HttpResponse, query: Query[RequestQuery]):
    rows = _perform(response, platform.own_requests, user=request.user, status=query.status)
    return _page(rows, query)


@router.post("/creation-requests/", response={201: RequestData})
def submit_request(request, response: HttpResponse, payload: RequestInput):
    if not throttle.allow(request, "organization_request"):
        raise HttpError(429, "Слишком много заявок. Повторите позже.")
    return 201, _perform(response, platform.submit_request, user=request.user, **payload.model_dump())


@router.post("/creation-requests/{request_id}/withdraw/", response=RequestData)
def withdraw_request(request, response: HttpResponse, request_id: RecordId):
    return _perform(response, platform.withdraw_request, user=request.user, request_id=request_id)


@router.get("/admin/creation-requests/", response=RequestPage, url_name="platform_admin_creation_requests")
def admin_requests(request, response: HttpResponse, query: Query[AdminRequestQuery]):
    rows = _perform(response, platform.admin_requests, user=request.user, status=query.status, q=query.q)
    return _page(rows, query)


@router.get("/admin/creation-requests/{request_id}/", response=RequestData)
def admin_request(request, response: HttpResponse, request_id: RecordId):
    return _perform(response, platform.admin_request, user=request.user, request_id=request_id)


@router.post("/admin/creation-requests/{request_id}/decision/", response=RequestData)
def decide_request(request, response: HttpResponse, request_id: RecordId, payload: DecisionInput):
    return _perform(response, platform.decide_request, user=request.user, request_id=request_id, **payload.model_dump())


@router.get("/creation-grants/", response=GrantPage, url_name="platform_creation_grants")
def grants(request, response: HttpResponse, query: Query[GrantQuery]):
    rows = _perform(response, platform.list_creation_grants, user=request.user, q=query.q)
    return _page(rows, query)


@router.post("/creation-grants/", response=GrantData)
def grant(request, response: HttpResponse, payload: GrantInput):
    return _perform(response, platform.grant_creation, user=request.user, username=payload.username)


@router.delete("/creation-grants/{user_id}/", response={204: None})
def revoke(request, response: HttpResponse, user_id: RecordId):
    _perform(response, platform.revoke_creation, user=request.user, user_id=user_id)
    return 204, None


@router.get("/creation-grant-candidates/", response=GrantCandidateData, url_name="platform_creation_grant_candidates")
def grant_candidates(request, response: HttpResponse, query: Query[SearchQuery]):
    return {"items": list(_perform(response, platform.grant_candidates, user=request.user, q=query.q))}
