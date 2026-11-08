from datetime import datetime
from typing import Annotated

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import StringConstraints, field_validator

from .models import ORGANIZATION_NAME_MAX_LENGTH
from .services import create_organization, visible_organizations

router = Router(auth=django_auth)


class OrganizationInput(Schema):
    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=ORGANIZATION_NAME_MAX_LENGTH,
        ),
    ]

    @field_validator("name")
    @classmethod
    def reject_null_characters(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Organization name cannot contain null characters")
        return value


class OrganizationOutput(Schema):
    id: int
    name: str
    created_at: datetime


@router.get("/", response=list[OrganizationOutput])
def list_organizations(request):
    return visible_organizations(request.user)


@router.post("/", response={201: OrganizationOutput}, url_name="create_organization")
def add_organization(request, payload: OrganizationInput):
    try:
        organization = create_organization(user=request.user, name=payload.name)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    return 201, organization


@router.get("/{org_id}/", response=OrganizationOutput)
def get_organization(request, org_id: int):
    return get_object_or_404(visible_organizations(request.user), pk=org_id)
