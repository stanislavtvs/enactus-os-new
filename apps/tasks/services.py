from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access
from apps.organizations.models import Organization
from apps.projects.models import ProjectMembership
from apps.projects.services import _can_view as can_view_project
from apps.projects.services import _context as project_context
from apps.projects.services import visible_projects

from .models import Task, TaskCollaborator

VIEW = "tasks.view"
CREATE = "tasks.create"
UPDATE = "tasks.update"
ASSIGN = "tasks.assign"
CHANGE_STATUS = "tasks.change_status"
ARCHIVE = "tasks.archive"

TRANSITIONS = {
    Task.Status.PENDING: (Task.Status.IN_PROGRESS, Task.Status.CANCELLED),
    Task.Status.IN_PROGRESS: (Task.Status.REVIEW, Task.Status.CANCELLED),
    Task.Status.REVIEW: (Task.Status.IN_PROGRESS, Task.Status.COMPLETED, Task.Status.CANCELLED),
}
REQUIRES_ASSIGNEE = {Task.Status.IN_PROGRESS, Task.Status.REVIEW, Task.Status.COMPLETED}
TERMINAL_STATUSES = {Task.Status.COMPLETED, Task.Status.CANCELLED}


class TaskConflict(Exception):
    """The task or one of its relations has an incompatible state."""


class TaskValidation(Exception):
    """The supplied task fields are inconsistent."""


def _context(project, department):
    if department is not None and not department.is_archived:
        return department
    return project_context(project) if project is not None else None


def can_view_context(access, project=None, department=None):
    return (
        (project is None or can_view_project(access, project))
        and access.allows(VIEW, _context(project, department))
    )


def _is_participant(access, task):
    member_id = access.membership.pk
    return member_id in (task.created_by_id, task.assignee_id) or task._my_collaboration


def _task_visible(access, task):
    if access.membership is None or (
        task.project_id is not None and not can_view_project(access, task.project)
    ):
        return False
    return access.allows(VIEW, _context(task.project, task.department)) or _is_participant(access, task)


def _task_rows(access, *, lock=False):
    organization = access.organization
    rows = organization.tasks.select_related(
        "created_by__user", "assignee__user", "department", "project__responsible_department",
    ).annotate(
        _my_collaboration=Exists(TaskCollaborator.objects.filter(
            organization=organization, task_id=OuterRef("pk"),
            organization_membership=access.membership, is_active=True,
        )),
        _active_project_assignee=Exists(ProjectMembership.objects.filter(
            organization=organization, project_id=OuterRef("project_id"),
            organization_membership_id=OuterRef("assignee_id"), is_active=True,
        )),
    )
    if lock:
        return rows.select_for_update(of=("self",))
    return rows.prefetch_related(Prefetch(
        "collaborators",
        queryset=TaskCollaborator.objects.filter(
            organization=organization, is_active=True, organization_membership__is_active=True,
            organization_membership__user__is_active=True,
        ).select_related("organization_membership__user").order_by("id"),
        to_attr="active_collaborators",
    ))


def visible_task(*, access, task_id):
    task = get_object_or_404(_task_rows(access), pk=task_id)
    if not _task_visible(access, task):
        raise Http404
    return task


def _visible_rows(access):
    if access.membership is None:
        return access.organization.tasks.none()
    projects = visible_projects(access=access)
    rows = _task_rows(access).filter(Q(project__isnull=True) | Q(project_id__in=[p.pk for p in projects]))
    return _task_access_filter(access, rows)


def _task_access_filter(access, rows):
    if access.allows(VIEW):
        return rows

    member_id = access.membership.pk
    department_ids = [
        department.pk for department in access.organization.departments.filter(is_archived=False)
        if access.allows(VIEW, department)
    ]
    return rows.filter(
        Q(created_by_id=member_id) | Q(assignee_id=member_id) | Q(_my_collaboration=True)
        | Q(department_id__in=department_ids)
        | (
            (Q(department__isnull=True) | Q(department__is_archived=True))
            & Q(project__responsible_department_id__in=department_ids)
        )
    )


def list_tasks(
    *, access, q="", status=None, project_id=None, department_id=None, mine=False,
    include_archived=False, page=1, page_size=20,
):
    rows = _visible_rows(access)
    if not include_archived:
        rows = rows.filter(is_archived=False)
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if status:
        rows = rows.filter(status=status)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if department_id is not None:
        rows = rows.filter(department_id=department_id)
    if mine:
        member_id = access.membership.pk
        rows = rows.filter(Q(assignee_id=member_id) | Q(_my_collaboration=True))
    offset = (page - 1) * page_size
    return rows.order_by("-id")[offset:offset + page_size], rows.count()


def can_open_tasks(access):
    if access.membership is None:
        return False
    if access.allows(VIEW) or any(
        access.allows(VIEW, department)
        for department in access.organization.departments.filter(is_archived=False)
    ):
        return True
    member_id = access.membership.pk
    personal = Q(created_by_id=member_id) | Q(assignee_id=member_id) | Q(_my_collaboration=True)
    return _task_rows(access).filter(personal).exists() and _visible_rows(access).filter(personal).exists()


def can_open_project_tasks(access, project):
    if access.membership is None or not can_view_project(access, project):
        return False
    if access.allows(VIEW, _context(project, None)) or any(
        access.allows(VIEW, department)
        for department in access.organization.departments.filter(is_archived=False)
    ):
        return True
    return _task_access_filter(access, _task_rows(access).filter(project=project)).exists()


def _can_create(access, project, department):
    return (
        (project is None or not project.is_archived)
        and can_view_context(access, project, department)
        and access.allows(CREATE, _context(project, department))
    )


def _can_assign(access, task):
    return (
        task.status not in TERMINAL_STATUSES
        and access.organization.task_assignment_mode == Organization.TaskAssignmentMode.DIRECT
        and access.allows(ASSIGN, _context(task.project, task.department))
        and (task.project_id is None or task.project.lead_membership_id == access.membership.pk)
    )


def _task_is_writable(task):
    return not task.is_archived and (task.project_id is None or not task.project.is_archived)


def task_permissions(*, access, task):
    writable = _task_is_writable(task)
    context = _context(task.project, task.department)
    return {
        "update": writable and access.allows(UPDATE, context),
        "assign": writable and _can_assign(access, task),
        "archive": writable and access.allows(ARCHIVE, context),
        "change_status": writable and access.allows(CHANGE_STATUS, context),
    }


def _active_candidates(access, task):
    members = access.organization.memberships.filter(is_active=True, user__is_active=True)
    if task.project_id is not None:
        members = members.filter(
            project_memberships__project=task.project, project_memberships__is_active=True,
        )
    return members


def _assignee_is_active(task):
    return (
        task.assignee is not None and task.assignee.is_active and task.assignee.user.is_active
        and (task.project_id is None or task._active_project_assignee)
    )


def allowed_statuses(*, access, task):
    if not _task_is_writable(task) or not access.allows(CHANGE_STATUS, _context(task.project, task.department)):
        return []
    choices = TRANSITIONS.get(task.status, ())
    if not choices or _assignee_is_active(task):
        return list(choices)
    return [status for status in choices if status not in REQUIRES_ASSIGNEE]


def task_options(*, access, task_id=None):
    task = visible_task(access=access, task_id=task_id) if task_id is not None else None
    departments = list(access.organization.departments.filter(is_archived=False).order_by("id"))
    projects = visible_projects(access=access)
    can_update = task is not None and task_permissions(access=access, task=task)["update"]
    assignees = []
    if task is not None and task_permissions(access=access, task=task)["assign"]:
        assignees = [
            {"id": member_id, "username": username}
            for member_id, username in _active_candidates(access, task).order_by("id").values_list("id", "user__username")
        ]
    return {
        "assignment_mode": access.organization.task_assignment_mode,
        "current_membership_id": access.membership.pk,
        "can_create_standalone": _can_create(access, None, None),
        "can_clear_department": can_update and access.allows(UPDATE, _context(task.project, None)),
        "departments": [
            {
                "id": department.pk, "name": department.name,
                "can_create": _can_create(access, None, department),
                "can_update": can_update and access.allows(UPDATE, department),
            }
            for department in departments
        ],
        "projects": [
            {
                "id": project.pk, "name": project.name,
                "can_create": _can_create(access, project, None),
                "department_ids": [
                    department.pk for department in departments if _can_create(access, project, department)
                ],
            }
            for project in projects if not project.is_archived
        ],
        "assignees": assignees,
    }


def _active_department(access, department_id):
    if department_id is None:
        return None
    department = get_object_or_404(access.organization.departments, pk=department_id)
    if department.is_archived:
        raise TaskConflict("Архивное подразделение нельзя назначить.")
    return department


def _validate_dates(planned_start_at, due_at):
    if any(value is not None and timezone.is_naive(value) for value in (planned_start_at, due_at)):
        raise TaskValidation("Укажите часовой пояс для дат задачи.")
    if planned_start_at is not None and due_at is not None and due_at < planned_start_at:
        raise TaskValidation("Срок задачи не может быть раньше планового начала.")


def _managed_task(user, org_id, task_id, action, *, allow_archived=False):
    access = organization_access(user=user, org_id=org_id, lock=True)
    task = get_object_or_404(_task_rows(access, lock=True), pk=task_id)
    if not _task_visible(access, task):
        raise Http404
    if not access.allows(action, _context(task.project, task.department)):
        raise PermissionDenied
    if not (allow_archived and task.is_archived) and not _task_is_writable(task):
        raise TaskConflict("Архивную задачу или задачу архивного проекта нельзя изменить.")
    return access, task


@transaction.atomic
def create_task(
    *, user, org_id, name, description="", priority=Task.Priority.NORMAL,
    project_id=None, department_id=None, planned_start_at=None, due_at=None,
):
    access = organization_access(user=user, org_id=org_id, lock=True)
    project = None
    if project_id is not None:
        project = get_object_or_404(
            access.organization.projects.select_related("responsible_department"), pk=project_id,
        )
        if not can_view_project(access, project):
            raise Http404
        if project.is_archived:
            raise TaskConflict("Нельзя добавить задачу в архивный проект.")
    department = _active_department(access, department_id)
    if not _can_create(access, project, department):
        raise PermissionDenied
    _validate_dates(planned_start_at, due_at)
    return access.organization.tasks.create(
        name=name, description=description, priority=priority, project=project,
        department=department, created_by=access.membership,
        planned_start_at=planned_start_at, due_at=due_at,
    )


@transaction.atomic
def update_task(*, user, org_id, task_id, changes):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    fields = []
    if "department_id" in changes:
        department = _active_department(access, changes["department_id"])
        target = _context(task.project, department)
        if not access.allows(UPDATE, target) or not (
            access.allows(VIEW, target) or _is_participant(access, task)
        ):
            raise PermissionDenied
        task.department = department
        fields.append("department")
    _validate_dates(changes.get("planned_start_at", task.planned_start_at), changes.get("due_at", task.due_at))
    for field in ("name", "description", "priority", "planned_start_at", "due_at"):
        if field in changes:
            setattr(task, field, changes[field])
            fields.append(field)
    if fields:
        task.save(update_fields=[*fields, "updated_at"])
    return task


@transaction.atomic
def archive_task(*, user, org_id, task_id):
    _, task = _managed_task(user, org_id, task_id, ARCHIVE, allow_archived=True)
    if not task.is_archived:
        task.is_archived = True
        task.save(update_fields=["is_archived", "updated_at"])


def _assignable_task(user, org_id, task_id):
    access, task = _managed_task(user, org_id, task_id, ASSIGN)
    if task.project_id is not None and task.project.lead_membership_id != access.membership.pk:
        raise PermissionDenied
    if access.organization.task_assignment_mode != Organization.TaskAssignmentMode.DIRECT:
        raise TaskConflict("Организация использует назначение через подразделение.")
    if task.status in TERMINAL_STATUSES:
        raise TaskConflict("У закрытой задачи нельзя менять исполнителей.")
    return access, task


def _candidate(access, task, member_id):
    member = get_object_or_404(access.organization.memberships, pk=member_id)
    if not _active_candidates(access, task).filter(pk=member.pk).exists():
        raise TaskConflict("Назначить можно только активного участника в области задачи.")
    return member


@transaction.atomic
def set_assignee(*, user, org_id, task_id, assignee_id):
    access, task = _assignable_task(user, org_id, task_id)
    if assignee_id is None and task.assignee_id is not None and task.status in REQUIRES_ASSIGNEE:
        raise TaskConflict("У задачи в этом статусе должен оставаться основной исполнитель.")
    assignee = None if assignee_id is None else _candidate(access, task, assignee_id)
    if assignee is not None and task.collaborators.filter(
        organization_membership=assignee, is_active=True,
    ).exists():
        raise TaskConflict("Основной исполнитель уже назначен соисполнителем.")
    if task.assignee_id != assignee_id:
        task.assignee = assignee
        task.save(update_fields=["assignee", "updated_at"])
    return task


@transaction.atomic
def set_collaborator(*, user, org_id, task_id, membership_id, include):
    access, task = _assignable_task(user, org_id, task_id)
    member = get_object_or_404(access.organization.memberships, pk=membership_id)
    collaborators = task.collaborators.filter(organization=access.organization, organization_membership=member)
    if not include:
        collaborators.filter(is_active=True).update(is_active=False)
        return
    _candidate(access, task, membership_id)
    if task.assignee_id == member.pk:
        raise TaskConflict("Основного исполнителя нельзя назначить соисполнителем.")
    collaborator, created = task.collaborators.get_or_create(
        organization=access.organization, organization_membership=member,
    )
    if not created:
        if collaborator.is_active:
            raise TaskConflict("Этот участник уже назначен соисполнителем.")
        collaborator.is_active = True
        collaborator.save(update_fields=["is_active"])


@transaction.atomic
def set_status(*, user, org_id, task_id, status):
    _, task = _managed_task(user, org_id, task_id, CHANGE_STATUS)
    if status == task.status:
        return task
    if status not in TRANSITIONS.get(task.status, ()):
        raise TaskConflict("Такой переход статуса недоступен.")
    if status in REQUIRES_ASSIGNEE and not _assignee_is_active(task):
        raise TaskConflict("Сначала назначьте основного исполнителя.")
    task.status = status
    task.completed_at = timezone.now() if status == Task.Status.COMPLETED else None
    task.save(update_fields=["status", "completed_at", "updated_at"])
    return task
