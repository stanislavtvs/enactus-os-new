from datetime import datetime
from typing import Annotated

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from ninja import PatchDict, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import ConfigDict, Field, StringConstraints, field_validator

from apps.departments.models import DepartmentMembership

from . import services
from .models import POSITION_TITLE_MAX_LENGTH, ROLE_NAME_MAX_LENGTH
from .policy import ORGANIZATION_ONLY, Permission

router = Router(auth=django_auth)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InputId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
ASSIGNMENT_CONFLICT_CONSTRAINTS = {
    "unique_active_role_assignment",
    "unique_active_position_assignment",
    "access_assignment_requires_active_parents",
}


class PermissionOutput(Schema):
    code: Permission
    label: str
    department_scoped: bool


class PermissionGrant(Schema):
    model_config = ConfigDict(extra="forbid")
    code: Permission
    include_descendants: bool = False


class RoleInput(Schema):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=ROLE_NAME_MAX_LENGTH,
        pattern=r"^[^\x00]+$",
    )]
    permissions: Annotated[list[PermissionGrant], Field(max_length=len(Permission))]

    @field_validator("name", "permissions")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class RoleOutput(Schema):
    id: int
    name: str
    is_archived: bool
    permissions: list[PermissionGrant]


class ScopeInput(Schema):
    model_config = ConfigDict(extra="forbid")
    organization_membership_id: InputId
    department_membership_id: InputId | None = None


class AssignmentInput(ScopeInput):
    role_id: InputId


class PositionInput(ScopeInput):
    title: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=POSITION_TITLE_MAX_LENGTH,
        pattern=r"^[^\x00]+$",
    )]


class ScopeOutput(Schema):
    id: int
    organization_membership_id: int
    department_membership_id: int | None
    revoked_at: datetime | None


class AssignmentOutput(ScopeOutput):
    role_id: int


class PositionOutput(ScopeOutput):
    title: str


class DepartmentSubject(Schema):
    id: int
    department_id: int
    name: str


class RoleSubject(Schema):
    id: int
    username: str
    department_memberships: list[DepartmentSubject]


class EffectiveOutput(Schema):
    permissions: list[Permission]


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except services.AccessConflict as exc:
        raise HttpError(409, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in ASSIGNMENT_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Состояние назначения изменилось.") from None


def _role_read(user, org_id):
    access = services.organization_access(user=user, org_id=org_id)
    _perform(services.require_role_read, access=access)
    return access.organization


@router.get("/{org_id}/permissions/", response=list[PermissionOutput])
def permission_catalog(request, org_id: RecordId):
    services.organization_access(user=request.user, org_id=org_id)
    return [
        {"code": permission, "label": permission.label, "department_scoped": permission not in ORGANIZATION_ONLY}
        for permission in Permission
    ]


@router.get("/{org_id}/permissions/effective/", response=EffectiveOutput)
def effective_permissions(request, org_id: RecordId, department_id: RecordId | None = None):
    access = services.organization_access(user=request.user, org_id=org_id)
    department = None
    if department_id is not None:
        department = get_object_or_404(access.organization.departments, pk=department_id, is_archived=False)
    return {"permissions": [permission for permission in Permission if access.allows(permission, department)]}


@router.get("/{org_id}/role-subjects/", response=list[RoleSubject])
def role_subjects(request, org_id: RecordId):
    organization = _role_read(request.user, org_id)
    departments = DepartmentMembership.objects.filter(
        organization=organization, is_active=True, department__is_archived=False,
        department__organization=organization,
    ).select_related("department").order_by("id")
    memberships = organization.memberships.filter(is_active=True, user__is_active=True).select_related(
        "user",
    ).prefetch_related(Prefetch("department_memberships", queryset=departments, to_attr="role_scopes")).order_by("id")
    return [
        {
            "id": membership.pk,
            "username": membership.user.get_username(),
            "department_memberships": [
                {"id": item.pk, "department_id": item.department_id, "name": item.department.name}
                for item in membership.role_scopes
            ],
        }
        for membership in memberships
    ]


@router.get("/{org_id}/roles/", response=list[RoleOutput], url_name="role_list")
def list_roles(request, org_id: RecordId):
    organization = _role_read(request.user, org_id)
    return organization.roles.prefetch_related("permissions").order_by("id")


@router.post("/{org_id}/roles/", response={201: RoleOutput})
def create_role(request, org_id: RecordId, payload: RoleInput):
    return 201, _perform(services.create_role, user=request.user, org_id=org_id, **payload.model_dump())


@router.patch("/{org_id}/roles/{role_id}/", response=RoleOutput)
def update_role(request, org_id: RecordId, role_id: RecordId, payload: PatchDict[RoleInput]):
    return _perform(services.update_role, user=request.user, org_id=org_id, role_id=role_id, changes=payload)


@router.delete("/{org_id}/roles/{role_id}/", response={204: None})
def archive_role(request, org_id: RecordId, role_id: RecordId):
    _perform(services.archive_role, user=request.user, org_id=org_id, role_id=role_id)
    return 204, None


@router.get("/{org_id}/role-assignments/", response=list[AssignmentOutput])
def list_role_assignments(request, org_id: RecordId):
    return _role_read(request.user, org_id).role_assignments.order_by("id")


@router.post("/{org_id}/role-assignments/", response={201: AssignmentOutput})
def assign_role(request, org_id: RecordId, payload: AssignmentInput):
    return 201, _perform(services.assign_role, user=request.user, org_id=org_id, **payload.model_dump())


@router.delete("/{org_id}/role-assignments/{assignment_id}/", response={204: None})
def revoke_role(request, org_id: RecordId, assignment_id: RecordId):
    _perform(services.revoke_role, user=request.user, org_id=org_id, assignment_id=assignment_id)
    return 204, None


@router.get("/{org_id}/positions/", response=list[PositionOutput])
def list_positions(request, org_id: RecordId):
    return _role_read(request.user, org_id).position_assignments.order_by("id")


@router.post("/{org_id}/positions/", response={201: PositionOutput})
def assign_position(request, org_id: RecordId, payload: PositionInput):
    return 201, _perform(services.assign_position, user=request.user, org_id=org_id, **payload.model_dump())


@router.delete("/{org_id}/positions/{position_id}/", response={204: None})
def revoke_position(request, org_id: RecordId, position_id: RecordId):
    _perform(services.revoke_position, user=request.user, org_id=org_id, position_id=position_id)
    return 204, None
