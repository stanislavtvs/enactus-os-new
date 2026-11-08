from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.access.policy import Permission
from apps.access.services import organization_access

from .models import Project, ProjectDepartmentLink


class ProjectConflict(Exception):
    """The project or one of its links has an incompatible state."""


class ProjectValidation(Exception):
    """The project fields do not form a valid record."""


def _context(project):
    department = project.responsible_department
    if department is not None and not department.is_archived:
        return department
    return None


def _can_view(access, project):
    return access.allows(Permission.PROJECTS_VIEW, _context(project))


def _require(access, action, department):
    if not access.allows(action, department):
        raise PermissionDenied


def _require_view(access, project):
    if not _can_view(access, project):
        raise Http404


def _projects(organization):
    return organization.projects.select_related(
        "responsible_department", "lead_membership__user",
    ).prefetch_related(Prefetch(
        "department_links",
        queryset=ProjectDepartmentLink.objects.filter(organization=organization)
        .select_related("department").order_by("department_id"),
    ))


def visible_projects(*, access):
    return [project for project in _projects(access.organization).order_by("id") if _can_view(access, project)]


def visible_project(*, access, project_id):
    project = get_object_or_404(_projects(access.organization), pk=project_id)
    _require_view(access, project)
    return project


def _can_create(access, department):
    return (
        access.allows(Permission.PROJECTS_VIEW, department)
        and access.allows(Permission.PROJECTS_CREATE, department)
        and (department is None or access.allows(Permission.PROJECTS_MANAGE_DEPARTMENTS, department))
    )


def can_open_projects(access):
    if access.allows(Permission.PROJECTS_VIEW):
        return True
    return any(
        access.allows(Permission.PROJECTS_VIEW, department)
        for department in access.organization.departments.filter(is_archived=False)
    )


def project_permissions(*, access, project):
    context = _context(project)
    return {
        name: not project.is_archived and access.allows(action, context)
        for name, action in (
            ("update", Permission.PROJECTS_UPDATE),
            ("archive", Permission.PROJECTS_ARCHIVE),
            ("manage_departments", Permission.PROJECTS_MANAGE_DEPARTMENTS),
            ("manage_lead", Permission.PROJECTS_MANAGE_LEAD),
        )
    }


def project_options(*, access, project_id=None):
    can_create_independent = _can_create(access, None)
    departments = []
    leader_contexts = can_create_independent and access.allows(Permission.PROJECTS_MANAGE_LEAD)
    for department in access.organization.departments.filter(is_archived=False).order_by("id"):
        can_create = _can_create(access, department)
        can_manage_lead = access.allows(Permission.PROJECTS_MANAGE_LEAD, department)
        can_manage_departments = access.allows(Permission.PROJECTS_MANAGE_DEPARTMENTS, department)
        leader_contexts = leader_contexts or (can_create and can_manage_lead)
        departments.append({
            "id": department.pk,
            "name": department.name,
            "can_create": can_create,
            "can_manage_departments": can_manage_departments,
            "can_manage_lead": can_manage_lead,
            "can_set_responsible": can_manage_departments and access.allows(Permission.PROJECTS_VIEW, department),
        })

    if project_id is not None:
        project = visible_project(access=access, project_id=project_id)
        leader_contexts = project_permissions(access=access, project=project)["manage_lead"]

    leaders = []
    if leader_contexts:
        leaders = [
            {"id": member_id, "username": username}
            for member_id, username in access.organization.memberships.filter(
                is_active=True, user__is_active=True,
            ).order_by("id").values_list("id", "user__username")
        ]

    can_manage_independent = access.allows(Permission.PROJECTS_MANAGE_DEPARTMENTS)
    return {
        "can_create_independent": can_create_independent,
        "departments": departments,
        "leaders": leaders,
        "can_manage_independent": can_manage_independent,
        "can_manage_independent_lead": access.allows(Permission.PROJECTS_MANAGE_LEAD),
        "can_set_independent": can_manage_independent and access.allows(Permission.PROJECTS_VIEW),
    }


def _department(access, department_id):
    if department_id is None:
        return None
    return get_object_or_404(access.organization.departments, pk=department_id)


def _require_active_department(department):
    if department is not None and department.is_archived:
        raise ProjectConflict("Нельзя назначить архивное подразделение.")


def _active_lead(access, membership_id):
    if membership_id is None:
        return None
    membership = get_object_or_404(
        access.organization.memberships.select_related("user"), pk=membership_id,
    )
    if not membership.is_active or not membership.user.is_active:
        raise ProjectConflict("Руководителем может быть только активный участник организации.")
    return membership


def _validate_dates(start_date, planned_end_date):
    if start_date is not None and planned_end_date is not None and planned_end_date < start_date:
        raise ProjectValidation("Плановая дата завершения не может быть раньше даты начала.")


def _managed_project(user, org_id, project_id, action, *, allow_archived=False):
    access = organization_access(user=user, org_id=org_id, lock=True)
    project = get_object_or_404(
        access.organization.projects.select_for_update(of=("self",)).select_related(
            "responsible_department",
        ), pk=project_id,
    )
    _require_view(access, project)
    _require(access, action, _context(project))
    if project.is_archived and not allow_archived:
        raise ProjectConflict("Архивный проект нельзя изменить.")
    return access, project


@transaction.atomic
def create_project(
    *, user, org_id, name, description="", status=Project.Status.DRAFT,
    start_date=None, planned_end_date=None, lead_membership_id=None, responsible_department_id=None,
):
    access = organization_access(user=user, org_id=org_id, lock=True)
    department = _department(access, responsible_department_id)
    _require_active_department(department)
    if not _can_create(access, department):
        raise PermissionDenied
    if lead_membership_id is not None:
        _require(access, Permission.PROJECTS_MANAGE_LEAD, department)
    lead = _active_lead(access, lead_membership_id)
    _validate_dates(start_date, planned_end_date)
    return access.organization.projects.create(
        name=name, description=description, status=status, start_date=start_date,
        planned_end_date=planned_end_date, lead_membership=lead,
        responsible_department=department, created_by=user,
    )


@transaction.atomic
def update_project(*, user, org_id, project_id, changes):
    _, project = _managed_project(user, org_id, project_id, Permission.PROJECTS_UPDATE)
    _validate_dates(
        changes.get("start_date", project.start_date),
        changes.get("planned_end_date", project.planned_end_date),
    )
    fields = []
    for field in ("name", "description", "status", "start_date", "planned_end_date"):
        if field in changes:
            setattr(project, field, changes[field])
            fields.append(field)
    if fields:
        project.save(update_fields=[*fields, "updated_at"])
    return project


@transaction.atomic
def archive_project(*, user, org_id, project_id):
    _, project = _managed_project(
        user, org_id, project_id, Permission.PROJECTS_ARCHIVE, allow_archived=True,
    )
    if not project.is_archived:
        project.is_archived = True
        project.save(update_fields=["is_archived", "updated_at"])


@transaction.atomic
def set_project_lead(*, user, org_id, project_id, lead_membership_id):
    access, project = _managed_project(user, org_id, project_id, Permission.PROJECTS_MANAGE_LEAD)
    lead = _active_lead(access, lead_membership_id)
    if project.lead_membership_id != lead_membership_id:
        project.lead_membership = lead
        project.save(update_fields=["lead_membership", "updated_at"])
    return project


@transaction.atomic
def set_responsible_department(*, user, org_id, project_id, responsible_department_id):
    access, project = _managed_project(
        user, org_id, project_id, Permission.PROJECTS_MANAGE_DEPARTMENTS,
    )
    department = _department(access, responsible_department_id)
    _require_active_department(department)
    _require(access, Permission.PROJECTS_MANAGE_DEPARTMENTS, department)
    _require(access, Permission.PROJECTS_VIEW, department)
    if project.responsible_department_id != responsible_department_id:
        project.responsible_department = department
        project.save(update_fields=["responsible_department", "updated_at"])
    return project


@transaction.atomic
def set_participating_department(*, user, org_id, project_id, department_id, include):
    access, project = _managed_project(
        user, org_id, project_id, Permission.PROJECTS_MANAGE_DEPARTMENTS,
    )
    department = _department(access, department_id)
    if include:
        _require_active_department(department)
    if not access.allows(Permission.PROJECTS_MANAGE_DEPARTMENTS):
        _require(access, Permission.PROJECTS_MANAGE_DEPARTMENTS, department)

    if include:
        project.department_links.get_or_create(
            project=project, organization=access.organization, department=department,
        )
    else:
        project.department_links.filter(
            organization=access.organization, department=department,
        ).delete()
