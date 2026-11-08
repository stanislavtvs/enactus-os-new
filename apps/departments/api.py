from datetime import datetime
from typing import Annotated

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import PatchDict, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import ConfigDict, Field, StringConstraints, field_validator

from . import services
from .models import DEPARTMENT_NAME_MAX_LENGTH

router = Router(auth=django_auth)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class DepartmentInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=DEPARTMENT_NAME_MAX_LENGTH,
            pattern=r"^[^\x00]+$",
        ),
    ]
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] = ""
    parent_id: Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)] | None = None

    @field_validator("name", "description")
    @classmethod
    def reject_explicit_null(cls, value):
        # PatchDict makes fields optional, but only parent_id may explicitly be null.
        if value is None:
            raise ValueError("Это поле не может быть пустым.")
        return value


class DepartmentOutput(Schema):
    id: int
    parent_id: int | None
    name: str
    description: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except services.DepartmentConflict as exc:
        raise HttpError(409, str(exc)) from None


@router.get("/{org_id}/departments/", response=list[DepartmentOutput], url_name="department_list")
def list_departments(request, org_id: RecordId):
    return services.visible_departments(user=request.user, org_id=org_id)


@router.post("/{org_id}/departments/", response={201: DepartmentOutput})
def create_department(request, org_id: RecordId, payload: DepartmentInput):
    department = _perform(
        services.create_department, user=request.user, org_id=org_id, **payload.model_dump()
    )
    return 201, department


@router.get("/{org_id}/departments/{department_id}/", response=DepartmentOutput)
def get_department(request, org_id: RecordId, department_id: RecordId):
    departments = services.visible_departments(user=request.user, org_id=org_id)
    return get_object_or_404(departments, pk=department_id)


@router.patch("/{org_id}/departments/{department_id}/", response=DepartmentOutput)
def update_department(
    request, org_id: RecordId, department_id: RecordId, payload: PatchDict[DepartmentInput]
):
    return _perform(
        services.update_department,
        user=request.user,
        org_id=org_id,
        department_id=department_id,
        changes=payload,
    )


@router.delete("/{org_id}/departments/{department_id}/", response={204: None})
def archive_department(request, org_id: RecordId, department_id: RecordId):
    _perform(
        services.archive_department, user=request.user, org_id=org_id, department_id=department_id
    )
    return 204, None


class DepartmentNodeActions(Schema):
    create: bool
    update: bool
    move: bool
    archive: bool
    open_members: bool


class DepartmentActionsOutput(Schema):
    root: dict[str, bool]
    nodes: dict[int, DepartmentNodeActions]


@router.get("/{org_id}/department-actions/", response=DepartmentActionsOutput, url_name="department_actions")
def get_department_actions(request, org_id: RecordId):
    return services.department_actions(user=request.user, org_id=org_id)
