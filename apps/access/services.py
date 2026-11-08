from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.departments.models import DepartmentMembership
from apps.organizations.services import visible_organizations

from .models import PositionAssignment, Role, RoleAssignment, RolePermission
from .policy import Access, ORGANIZATION_ONLY, Permission


class AccessConflict(Exception):
    """A role change conflicts with the current assignments."""


def organization_access(*, user, org_id, lock=False):
    organizations = visible_organizations(user)
    if lock:
        organizations = organizations.select_for_update(of=("self",))
    return Access(user, get_object_or_404(organizations, pk=org_id))


def require_role_read(access):
    if not (access.allows(Permission.ROLES_READ) or access.allows(Permission.ROLES_MANAGE)):
        raise PermissionDenied


def _role_manager(user, org_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    if not access.allows(Permission.ROLES_MANAGE):
        raise PermissionDenied
    return access


def _role(access, role_id):
    return get_object_or_404(access.organization.roles.select_for_update(), pk=role_id)


def _grants(role):
    return list(role.permissions.values("code", "include_descendants"))


def _check_grant_coverage(access, permissions, department=None):
    if access.is_owner:
        return
    for grant in permissions:
        if grant["code"] == Permission.ROLES_MANAGE or not access.allows(
            grant["code"], department, include_descendants=grant["include_descendants"],
        ):
            raise PermissionDenied


def _check_role_edit(access, role):
    if access.is_owner:
        return
    if role.assignments.filter(revoked_at__isnull=True).exists():
        raise PermissionDenied
    _check_grant_coverage(access, _grants(role))


def _validate_grants(permissions):
    codes = [grant["code"] for grant in permissions]
    if len(set(codes)) != len(codes):
        raise AccessConflict("Укажите каждое разрешение только один раз.")
    for grant in permissions:
        code = grant["code"]
        if code not in Permission.values or (code in ORGANIZATION_ONLY and grant["include_descendants"]):
            raise AccessConflict("Это разрешение нельзя сохранить с указанной областью действия.")


def _replace_grants(role, permissions):
    role.permissions.all().delete()
    RolePermission.objects.bulk_create([RolePermission(role=role, **grant) for grant in permissions])


@transaction.atomic
def create_role(*, user, org_id, name, permissions):
    access = _role_manager(user, org_id)
    _validate_grants(permissions)
    _check_grant_coverage(access, permissions)
    role = access.organization.roles.create(name=name)
    _replace_grants(role, permissions)
    return role


@transaction.atomic
def update_role(*, user, org_id, role_id, changes):
    access = _role_manager(user, org_id)
    role = _role(access, role_id)
    if role.is_archived:
        raise AccessConflict("Архивную роль нельзя изменить.")
    _check_role_edit(access, role)

    if "permissions" in changes:
        permissions = changes["permissions"]
        _validate_grants(permissions)
        _check_grant_coverage(access, permissions)
        if any(grant["code"] in ORGANIZATION_ONLY for grant in permissions) and role.assignments.filter(
            revoked_at__isnull=True, department_membership__isnull=False,
        ).exists():
            raise AccessConflict("Эту роль уже используют в области подразделения.")
        _replace_grants(role, permissions)
    if "name" in changes:
        role.name = changes["name"]
        role.save(update_fields=["name"])
    return role


@transaction.atomic
def archive_role(*, user, org_id, role_id):
    access = _role_manager(user, org_id)
    role = _role(access, role_id)
    _check_role_edit(access, role)
    if not role.is_archived:
        role.is_archived = True
        role.save(update_fields=["is_archived"])
        role.assignments.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


def _check_not_self(access, membership_id):
    if not access.is_owner and access.membership.pk == membership_id:
        raise PermissionDenied


def _scope(access, organization_membership_id, department_membership_id):
    _check_not_self(access, organization_membership_id)
    organization_member = get_object_or_404(
        access.organization.memberships.select_for_update(of=("self",)),
        pk=organization_membership_id, is_active=True, user__is_active=True,
    )
    department_member = None
    if department_membership_id is not None:
        department_member = get_object_or_404(
            DepartmentMembership.objects.select_for_update(of=("self",)).select_related("department"),
            organization=access.organization, organization_membership=organization_member,
            pk=department_membership_id, is_active=True, department__is_archived=False,
            department__organization=access.organization,
        )
    return organization_member, department_member


@transaction.atomic
def assign_role(*, user, org_id, role_id, organization_membership_id, department_membership_id=None):
    access = _role_manager(user, org_id)
    role = _role(access, role_id)
    if role.is_archived:
        raise AccessConflict("Архивную роль нельзя назначить.")

    member, department_member = _scope(access, organization_membership_id, department_membership_id)
    department = department_member.department if department_member else None
    permissions = _grants(role)
    if department is not None and any(grant["code"] in ORGANIZATION_ONLY for grant in permissions):
        raise AccessConflict("Эту роль можно назначить только на уровне организации.")
    _check_grant_coverage(access, permissions, department)
    assignments = access.organization.role_assignments
    scope = {"role": role, "organization_membership": member, "department_membership": department_member}
    if assignments.filter(**scope, revoked_at__isnull=True).exists():
        raise AccessConflict("Эта роль уже назначена в выбранной области.")
    return assignments.create(**scope)


@transaction.atomic
def revoke_role(*, user, org_id, assignment_id):
    access = _role_manager(user, org_id)
    assignment = get_object_or_404(
        access.organization.role_assignments.select_related("role", "department_membership__department"),
        pk=assignment_id,
    )
    _check_not_self(access, assignment.organization_membership_id)
    department = assignment.department_membership.department if assignment.department_membership_id else None
    _check_grant_coverage(access, _grants(assignment.role), department)
    if assignment.revoked_at is None:
        assignment.revoked_at = timezone.now()
        assignment.save(update_fields=["revoked_at"])


@transaction.atomic
def assign_position(*, user, org_id, title, organization_membership_id, department_membership_id=None):
    access = _role_manager(user, org_id)
    member, department_member = _scope(access, organization_membership_id, department_membership_id)
    positions = access.organization.position_assignments
    scope = {"title": title, "organization_membership": member, "department_membership": department_member}
    if positions.filter(**scope, revoked_at__isnull=True).exists():
        raise AccessConflict("Эта должность уже назначена в выбранной области.")
    return positions.create(**scope)


@transaction.atomic
def revoke_position(*, user, org_id, position_id):
    access = _role_manager(user, org_id)
    position = get_object_or_404(access.organization.position_assignments, pk=position_id)
    _check_not_self(access, position.organization_membership_id)
    if position.revoked_at is None:
        position.revoked_at = timezone.now()
        position.save(update_fields=["revoked_at"])
