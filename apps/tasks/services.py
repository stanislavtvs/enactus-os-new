from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Case, Exists, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access
from apps.organizations.modules import PROJECTS, TASKS, enabled, require_module
from apps.audit.producer import affected_ids, record_change, snapshot
from apps.notifications.producers import emit
from apps.organizations.models import Organization
from apps.projects.models import ProjectMembership
from apps.projects.services import _can_view as can_view_project
from apps.projects.services import _context as project_context
from apps.projects.services import visible_projects

from . import priorities, statuses
from .models import Task, TaskCollaborator, TaskDependency, TaskDistribution

VIEW = "tasks.view"
CREATE = "tasks.create"
UPDATE = "tasks.update"
ASSIGN = "tasks.assign"
ARCHIVE = "tasks.archive"

REQUIRES_ASSIGNEE = {Task.Status.IN_PROGRESS, Task.Status.REVIEW, Task.Status.COMPLETED}
TERMINAL_STATUSES = {Task.Status.COMPLETED, Task.Status.CANCELLED}
INHERIT_SCOPE = object()
TASK_AUDIT_FIELDS = (
    "name", "description", "priority_level_id", "priority", "planned_start_at", "due_at",
    "department_id", "parent_id", "status", "workflow_status_id",
)


class TaskConflict(Exception):
    """The task or one of its relations has an incompatible state."""


class TaskValidation(Exception):
    """The supplied task fields are inconsistent."""


def _context(project, department):
    if department is not None and not department.is_archived:
        return department
    return project_context(project) if project is not None else None


def _audit_task(user, task, action, *, before=None, fields=(), metadata=None, force=False):
    context = _context(task.project, task.department)
    record_change(
        user=user, action=f"task.{action}", instance=task, before=before, fields=fields,
        department_id=context.pk if context is not None else None, project_id=task.project_id,
        metadata=metadata, force=force,
    )


def _related_audit_scope(*tasks):
    related = [task for task in tasks if task is not None]
    departments = [_context(task.project, task.department) for task in related]
    return {
        "affected_department_ids": affected_ids(*(department.pk for department in departments if department)),
        "affected_project_ids": affected_ids(*(task.project_id for task in related)),
        "organization_scope": any(department is None for department in departments),
    }


def can_view_context(access, project=None, department=None):
    return (
        task_scope_enabled(access.organization, project.pk if project is not None else None)
        and (project is None or can_view_project(access, project))
        and access.allows(VIEW, _context(project, department))
    )


def task_scope_enabled(organization, project_id):
    return enabled(organization, TASKS) and (project_id is None or enabled(organization, PROJECTS))


def filter_enabled_tasks(rows, organization, *, project_field="project_id"):
    if not enabled(organization, TASKS):
        return rows.none()
    if not enabled(organization, PROJECTS):
        return rows.filter(**{f"{project_field}__isnull": True})
    return rows


def _is_participant(access, task):
    member_id = access.membership.pk
    if member_id in (task.created_by_id, task.assignee_id):
        return True
    if hasattr(task, "_my_collaboration"):
        return task._my_collaboration
    return task.collaborators.filter(organization_membership_id=member_id, is_active=True).exists()


def _task_visible(access, task):
    if access.membership is None or not task_scope_enabled(access.organization, task.project_id):
        return False
    if task.project_id is not None:
        if hasattr(task, "_active_project_viewer"):
            task.project._active_team_member = task._active_project_viewer
        if not can_view_project(access, task.project):
            return False
    return access.allows(VIEW, _context(task.project, task.department)) or _is_participant(access, task)


def _task_rows(access, *, lock=False):
    organization = access.organization
    unresolved_dependencies = TaskDependency.objects.filter(
        organization=organization, task_id=OuterRef("pk"),
    ).exclude(predecessor__status=Task.Status.COMPLETED)
    unfinished_children = filter_enabled_tasks(
        Task.objects.filter(organization=organization, parent_id=OuterRef("pk"), is_archived=False),
        organization,
    ).exclude(status__in=TERMINAL_STATUSES)
    visible_dependency_warnings = filter_enabled_tasks(
        unresolved_dependencies.filter(kind=TaskDependency.Kind.INFORMATIONAL),
        organization, project_field="predecessor__project_id",
    )
    rows = filter_enabled_tasks(organization.tasks.all(), organization).select_related(
        "created_by__user", "assignee__user", "department", "project__responsible_department",
        "reviewer__user", "organization",
        "priority_level", "workflow_status",
    ).annotate(
        _my_collaboration=_collaboration_exists(access),
        _active_project_assignee=Exists(ProjectMembership.objects.filter(
            organization=organization, project_id=OuterRef("project_id"),
            organization_membership_id=OuterRef("assignee_id"), is_active=True,
        )),
        _active_project_viewer=Exists(ProjectMembership.objects.filter(
            organization=organization, project_id=OuterRef("project_id"),
            organization_membership=access.membership, is_active=True,
        )),
        _distribution_pending=Exists(TaskDistribution.objects.filter(
            organization=organization, task_id=OuterRef("pk"), status=TaskDistribution.Status.PENDING,
        )),
        has_unfinished_children=Exists(unfinished_children),
        _strict_dependencies_unresolved=Exists(unresolved_dependencies.filter(
            kind=TaskDependency.Kind.STRICT,
        )),
        _informational_dependencies_unresolved=Exists(visible_dependency_warnings),
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
    require_module(access.organization, TASKS)
    task = get_object_or_404(_task_rows(access), pk=task_id)
    if not _task_visible(access, task):
        raise Http404
    return task


def _collaboration_exists(access):
    return Exists(TaskCollaborator.objects.filter(
        organization=access.organization, task_id=OuterRef("pk"),
        organization_membership=access.membership, is_active=True,
    ))


def _visible_rows(access, rows=None):
    if access.membership is None or not enabled(access.organization, TASKS):
        return (rows if rows is not None else access.organization.tasks).none()
    projects = visible_projects(access=access) if enabled(access.organization, PROJECTS) else []
    if rows is None:
        rows = _task_rows(access)
    rows = rows.filter(Q(project__isnull=True) | Q(project_id__in=[p.pk for p in projects]))
    return _task_access_filter(access, rows)


def visible_task_deadlines(*, access, start, end, project_id=None, department_id=None, include_archived=False):
    rows = access.organization.tasks.filter(due_at__gte=start, due_at__lt=end)
    if not include_archived:
        rows = rows.filter(is_archived=False)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if department_id is not None:
        rows = rows.filter(department_id=department_id)
    rows = rows.alias(_my_collaboration=_collaboration_exists(access)).select_related("workflow_status")
    return _visible_rows(access, rows).order_by("due_at", "pk")


def visible_parent_names(access, tasks):
    parent_ids = {task.parent_id for task in tasks if task.parent_id is not None}
    if not parent_ids:
        return {}
    return dict(_visible_rows(access).filter(pk__in=parent_ids).values_list("id", "name"))


def visible_parent_tasks(access, tasks):
    parent_ids = {task.parent_id for task in tasks if task.parent_id is not None}
    if not parent_ids:
        return {}
    rows = _visible_rows(access).filter(pk__in=parent_ids).prefetch_related(None)
    return {parent.pk: parent for parent in rows}


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
    include_archived=False, distribution_pending=None, queue_task_ids=None, page=1, page_size=20,
    priority_id=None, workflow_status_id=None, sort=None,
):
    rows = (
        _visible_rows(access) if queue_task_ids is None
        else _task_rows(access).filter(pk__in=queue_task_ids)
    )
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
    if distribution_pending is not None:
        rows = rows.filter(_distribution_pending=distribution_pending)
    if priority_id is not None:
        legacy_code = access.organization.task_priorities.filter(
            pk=priority_id, code__in=priorities.LEGACY_PRIORITIES,
        ).values("code")[:1]
        rows = rows.filter(
            Q(priority_level_id=priority_id)
            | Q(priority_level_id__isnull=True, priority=Subquery(legacy_code)),
        )
    if workflow_status_id is not None:
        fallback_stage = access.organization.task_statuses.filter(
            pk=workflow_status_id, is_default=True, is_active=True,
        ).values("stage")[:1]
        rows = rows.filter(
            Q(workflow_status_id=workflow_status_id)
            | Q(workflow_status__isnull=True, status=Subquery(fallback_stage)),
        )
    if mine:
        member_id = access.membership.pk
        rows = rows.filter(Q(assignee_id=member_id) | Q(_my_collaboration=True))
    ordering = ("-id",)
    if sort == "priority":
        legacy_rank = access.organization.task_priorities.filter(
            code=OuterRef("priority"),
        ).values("rank")[:1]
        rows = rows.annotate(_priority_rank=Coalesce(
            "priority_level__rank", Subquery(legacy_rank),
            Case(When(priority="high", then=Value(300)), When(priority="low", then=Value(100)), default=Value(200)),
        ))
        ordering = ("-_priority_rank", "-id")
    elif sort == "status":
        default_order = access.organization.task_statuses.filter(
            stage=OuterRef("status"), is_default=True, is_active=True,
        ).values("sort_order")[:1]
        rows = rows.annotate(_status_order=Coalesce(
            "workflow_status__sort_order", Subquery(default_order), Value(0),
        ))
        ordering = ("_status_order", "-id")
    offset = (page - 1) * page_size
    return rows.order_by(*ordering)[offset:offset + page_size], rows.count()


def can_open_tasks(access):
    if access.membership is None or not enabled(access.organization, TASKS):
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
    if (
        access.membership is None
        or not task_scope_enabled(access.organization, project.pk)
        or not can_view_project(access, project)
    ):
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
        and task.status != Task.Status.REVIEW
        and not _has_pending_distribution(task)
        and access.organization.task_assignment_mode == Organization.TaskAssignmentMode.DIRECT
        and access.allows(ASSIGN, _context(task.project, task.department))
        and _is_direct_project_lead(access, task.project)
    )


def _is_direct_project_lead(access, project):
    return project is None or project.lead_membership_id == access.membership.pk


def _task_is_writable(task):
    return not task.is_archived and (task.project_id is None or not task.project.is_archived)


def _has_pending_distribution(task):
    if hasattr(task, "_distribution_pending"):
        return task._distribution_pending
    return task.distributions.filter(status=TaskDistribution.Status.PENDING).exists()


def task_permissions(*, access, task):
    writable = _task_is_writable(task) and _task_visible(access, task)
    context = _context(task.project, task.department)
    return {
        "update": writable and access.allows(UPDATE, context),
        "assign": writable and _can_assign(access, task),
        "archive": writable and access.allows(ARCHIVE, context),
        "change_status": writable and statuses.can_attempt_transition(access, task, context),
    }


def _active_candidates(access, task):
    return _assignment_candidates(access, task.project)


def _assignment_candidates(access, project):
    members = access.organization.memberships.filter(is_active=True, user__is_active=True)
    if project is not None:
        members = members.filter(
            project_memberships__project=project, project_memberships__is_active=True,
        )
    return members


def _assignee_is_active(task):
    return (
        task.assignee is not None and task.assignee.is_active and task.assignee.user.is_active
        and (task.project_id is None or task._active_project_assignee)
    )


def has_blocking_dependencies(task):
    return task.status == Task.Status.PENDING and task._strict_dependencies_unresolved


def has_dependency_warnings(task):
    return task._informational_dependencies_unresolved


def require_no_blocking_dependencies(task):
    if has_blocking_dependencies(task):
        raise TaskConflict("Сначала завершите блокирующие предшествующие задачи.")


def allowed_workflow_statuses(*, access, task):
    if not _task_is_writable(task) or not _task_visible(access, task):
        return []
    context = _context(task.project, task.department)
    if not statuses.can_attempt_transition(access, task, context):
        return []
    source = statuses.task_workflow_status(access, task)
    if source is None:
        return []
    return [
        rule.to_status
        for rule in statuses.transition_rules(access, source.pk)
        if statuses.can_execute_rule(access, task, context, rule)
        and _manual_stage_conflict(task, rule.to_status.stage) is None
    ]


def allowed_statuses(*, access, task, workflow_choices=None):
    if workflow_choices is None:
        workflow_choices = allowed_workflow_statuses(access=access, task=task)
    return [status.stage for status in workflow_choices if status.is_default and status.stage != task.status]


def _manual_stage_conflict(task, target_stage):
    if target_stage == task.status:
        return None
    if not statuses.stage_transition_allowed(task.status, target_stage):
        return "Такой переход системного этапа недоступен."
    if target_stage == Task.Status.IN_PROGRESS and has_blocking_dependencies(task):
        return "Сначала завершите блокирующие предшествующие задачи."
    if target_stage in REQUIRES_ASSIGNEE and not _assignee_is_active(task):
        return "Сначала назначьте основного исполнителя."
    return None


def task_options(*, access, task_id=None):
    require_module(access.organization, TASKS)
    task = visible_task(access=access, task_id=task_id) if task_id is not None else None
    departments = list(access.organization.departments.filter(is_archived=False).order_by("id"))
    projects = visible_projects(access=access) if enabled(access.organization, PROJECTS) else []
    can_update = task is not None and task_permissions(access=access, task=task)["update"]
    assignees = []
    if task is not None and task_permissions(access=access, task=task)["assign"]:
        assignees = [
            {"id": member_id, "username": username}
            for member_id, username in _active_candidates(access, task).order_by("id").values_list("id", "user__username")
        ]
    return {
        "assignment_mode": access.organization.task_assignment_mode,
        "priorities": priorities.available_priorities(access.organization),
        "statuses": statuses.available_statuses(access.organization),
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
    access = organization_access(user=user, org_id=org_id, lock=True, module=TASKS)
    task = get_object_or_404(_task_rows(access, lock=True), pk=task_id)
    if not _task_visible(access, task):
        raise Http404
    if action is not None and not access.allows(action, _context(task.project, task.department)):
        raise PermissionDenied
    if not (allow_archived and task.is_archived) and not _task_is_writable(task):
        raise TaskConflict("Архивную задачу или задачу архивного проекта нельзя изменить.")
    return access, task


@transaction.atomic
def create_task(
    *, user, org_id, name, description="", priority=priorities.UNSET, priority_id=priorities.UNSET,
    project_id=INHERIT_SCOPE, department_id=INHERIT_SCOPE, parent_id=None,
    planned_start_at=None, due_at=None, assignee_id=None,
):
    access = organization_access(user=user, org_id=org_id, lock=True, module=TASKS)
    parent, project, department = _creation_context(
        access, project_id=project_id, department_id=department_id, parent_id=parent_id, lock=True,
    )
    if not _can_create(access, project, department):
        raise PermissionDenied
    _validate_dates(planned_start_at, due_at)
    level = priorities.resolve_task_priority(access.organization, priority=priority, priority_id=priority_id)
    initial_status = statuses.default_statuses(access).get(Task.Status.PENDING)
    if initial_status is None:
        raise TaskConflict("Для начального этапа нет действующего статуса по умолчанию.")
    task = access.organization.tasks.create(
        name=name, description=description, priority=priorities.legacy_priority_code(level),
        priority_level=level, project=project,
        department=department, parent=parent, created_by=access.membership, workflow_status=initial_status,
        planned_start_at=planned_start_at, due_at=due_at,
    )
    _audit_task(
        user, task, "created", fields=TASK_AUDIT_FIELDS,
        metadata=_related_audit_scope(parent) if parent is not None else None,
    )
    if assignee_id is not None:
        task = set_assignee(user=user, org_id=org_id, task_id=task.pk, assignee_id=assignee_id)
    return task


def _creation_context(access, *, project_id, department_id, parent_id, lock):
    require_module(access.organization, TASKS)
    parent = _parent_for_change(access, parent_id, require_writable=True, lock=lock)
    if project_id is INHERIT_SCOPE:
        project_id = parent.project_id if parent else None
    if department_id is INHERIT_SCOPE:
        department_id = (
            parent.department_id if parent and parent.department_id and not parent.department.is_archived else None
        )
    project = None
    if project_id is not None:
        require_module(access.organization, PROJECTS)
        project = get_object_or_404(
            access.organization.projects.select_related("responsible_department"), pk=project_id,
        )
        if not can_view_project(access, project):
            raise Http404
        if project.is_archived:
            raise TaskConflict("Нельзя добавить задачу в архивный проект.")
    department = _active_department(access, department_id)
    return parent, project, department


def task_assignee_options(
    *, access, project_id=INHERIT_SCOPE, department_id=INHERIT_SCOPE, parent_id=None,
):
    _, project, department = _creation_context(
        access, project_id=project_id, department_id=department_id, parent_id=parent_id, lock=False,
    )
    assignment_mode = access.organization.task_assignment_mode
    can_assign = (
        assignment_mode == Organization.TaskAssignmentMode.DIRECT
        and _can_create(access, project, department)
        and access.allows(ASSIGN, _context(project, department))
        and _is_direct_project_lead(access, project)
    )
    candidates = []
    if can_assign:
        candidates = [
            {"id": member_id, "username": username}
            for member_id, username in _assignment_candidates(access, project)
            .order_by("id").values_list("id", "user__username")
        ]
    return {"assignment_mode": assignment_mode, "can_assign": can_assign, "candidates": candidates}


def _parent_for_change(access, parent_id, *, require_writable, lock=True):
    if parent_id is None:
        return None
    parent = get_object_or_404(_task_rows(access, lock=lock), pk=parent_id)
    if not _task_visible(access, parent):
        raise Http404
    if not access.allows(UPDATE, _context(parent.project, parent.department)):
        raise PermissionDenied
    if require_writable and not _task_is_writable(parent):
        raise TaskConflict("Архивная задача не может стать родительской.")
    return parent


@transaction.atomic
def set_task_parent(*, user, org_id, task_id, parent_id):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    if parent_id == task.pk:
        raise TaskConflict("Задача не может быть своим родителем.")
    old_parent = _parent_for_change(access, task.parent_id, require_writable=False)
    parent = _parent_for_change(access, parent_id, require_writable=True)
    if parent is not None:
        parents = dict(access.organization.tasks.values_list("id", "parent_id"))
        visited = {task.pk}
        current = parent.pk
        while current is not None:
            if current in visited:
                raise TaskConflict("Такой перенос создаёт цикл подзадач.")
            visited.add(current)
            current = parents[current]
    if task.parent_id != parent_id:
        before = snapshot(task, ("parent_id",))
        task.parent = parent
        task.save(update_fields=["parent", "updated_at"])
        _audit_task(
            user, task, "parent_changed", before=before, fields=("parent_id",),
            metadata=_related_audit_scope(old_parent, parent),
        )
    return task


@transaction.atomic
def update_task(*, user, org_id, task_id, changes):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    before = snapshot(task, TASK_AUDIT_FIELDS)
    previous_context = _context(task.project, task.department)
    fields = []
    if "department_id" in changes:
        if changes["department_id"] != task.department_id:
            if task.status == Task.Status.REVIEW:
                raise TaskConflict("Нельзя менять подразделение во время проверки.")
            if _has_pending_distribution(task):
                if task.department_id is None or not task.department.is_archived:
                    raise TaskConflict("Нельзя менять подразделение во время распределения.")
                _cancel_distribution(task, access.membership)
        department = _active_department(access, changes["department_id"])
        target = _context(task.project, department)
        if not access.allows(UPDATE, target) or not (
            access.allows(VIEW, target) or _is_participant(access, task)
        ):
            raise PermissionDenied
        task.department = department
        fields.append("department")
    _validate_dates(changes.get("planned_start_at", task.planned_start_at), changes.get("due_at", task.due_at))
    if "priority" in changes or "priority_id" in changes:
        level = priorities.resolve_task_priority(
            access.organization, priority=changes.get("priority", priorities.UNSET),
            priority_id=changes.get("priority_id", priorities.UNSET),
        )
        task.priority_level = level
        task.priority = priorities.legacy_priority_code(level)
        fields.extend(["priority_level", "priority"])
    for field in ("name", "description", "planned_start_at", "due_at"):
        if field in changes:
            setattr(task, field, changes[field])
            fields.append(field)
    if fields:
        task.save(update_fields=[*fields, "updated_at"])
        metadata = None
        if before["department_id"] != task.department_id:
            current_context = _context(task.project, task.department)
            metadata = {
                "affected_department_ids": affected_ids(
                    previous_context.pk if previous_context else None,
                    current_context.pk if current_context else None,
                ),
                "organization_scope": previous_context is None or current_context is None,
            }
        _audit_task(user, task, "updated", before=before, fields=TASK_AUDIT_FIELDS, metadata=metadata)
    return task


@transaction.atomic
def archive_task(*, user, org_id, task_id):
    access, task = _managed_task(user, org_id, task_id, ARCHIVE, allow_archived=True)
    if not task.is_archived:
        task.is_archived = True
        task.save(update_fields=["is_archived", "updated_at"])
        _cancel_distribution(task, access.membership)
        _audit_task(user, task, "archived", before={"is_archived": False}, fields=("is_archived",))


def _assignable_task(user, org_id, task_id):
    access, task = _managed_task(user, org_id, task_id, ASSIGN)
    if not _is_direct_project_lead(access, task.project):
        raise PermissionDenied
    if access.organization.task_assignment_mode != Organization.TaskAssignmentMode.DIRECT:
        raise TaskConflict("Организация использует назначение через подразделение.")
    if task.status in TERMINAL_STATUSES:
        raise TaskConflict("У закрытой задачи нельзя менять исполнителей.")
    if task.status == Task.Status.REVIEW:
        raise TaskConflict("Нельзя менять исполнителей во время проверки.")
    if _has_pending_distribution(task):
        raise TaskConflict("Задачу ожидает распределение подразделением.")
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
    if assignee is not None and assignee.pk == task.reviewer_id:
        raise TaskConflict("Назначенный проверяющий не может быть исполнителем этой задачи.")
    if assignee is not None and task.collaborators.filter(
        organization_membership=assignee, is_active=True,
    ).exists():
        raise TaskConflict("Основной исполнитель уже назначен соисполнителем.")
    if task.assignee_id != assignee_id:
        previous = task.assignee
        before = snapshot(task, ("assignee_id",))
        task.assignee = assignee
        task.save(update_fields=["assignee", "updated_at"])
        change = task.distributions.create(
            organization=access.organization, department=task.department,
            requested_by=access.membership, resolved_by=access.membership,
            mode=Organization.TaskAssignmentMode.DIRECT, assignee=assignee,
            status=TaskDistribution.Status.ASSIGNED if assignee else TaskDistribution.Status.CLEARED,
            resolved_at=timezone.now(),
        )
        _audit_task(
            user, task, "assigned" if assignee else "unassigned", before=before, fields=("assignee_id",),
            metadata={"distribution_id": change.pk, "mode": change.mode},
        )
        if previous is not None:
            emit(user, "task.unassigned", "task", task, [previous.user_id], key=f"task-assignment:{change.pk}:removed")
        if assignee is not None:
            emit(user, "task.assigned", "task", task, [assignee.user_id], key=f"task-assignment:{change.pk}:added")
    return task


@transaction.atomic
def set_collaborator(*, user, org_id, task_id, membership_id, include):
    access, task = _assignable_task(user, org_id, task_id)
    member = get_object_or_404(access.organization.memberships, pk=membership_id)
    collaborators = task.collaborators.filter(organization=access.organization, organization_membership=member)
    if not include:
        if collaborators.filter(is_active=True).update(is_active=False):
            _audit_task(user, task, "collaborator_removed", metadata={"membership_id": member.pk}, force=True)
            emit(user, "task.unassigned", "task", task, [member.user_id])
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
    _audit_task(user, task, "collaborator_added", metadata={"membership_id": member.pk}, force=True)
    emit(user, "task.collaborator_added", "task", task, [member.user_id])


@transaction.atomic
def set_status(*, user, org_id, task_id, status):
    access, task = _managed_task(user, org_id, task_id, None)
    if not statuses.can_attempt_transition(access, task, _context(task.project, task.department)):
        raise PermissionDenied
    if status == task.status:
        return task
    target = statuses.default_statuses(access).get(status)
    if target is None:
        raise TaskConflict("У целевого этапа нет действующего статуса по умолчанию.")
    return _set_workflow_status(access, task, target)


@transaction.atomic
def set_workflow_status(*, user, org_id, task_id, status_id, expected_status_id):
    access, task = _managed_task(user, org_id, task_id, None)
    context = _context(task.project, task.department)
    if not statuses.can_attempt_transition(access, task, context):
        raise PermissionDenied
    source = statuses.task_workflow_status(access, task)
    if source is None or source.pk != expected_status_id:
        raise TaskConflict("Статус задачи изменился. Обновите карточку и повторите действие.")
    target = get_object_or_404(access.organization.task_statuses, pk=status_id)
    return _set_workflow_status(access, task, target)


def _set_workflow_status(access, task, target):
    context = _context(task.project, task.department)
    if not statuses.can_attempt_transition(access, task, context):
        raise PermissionDenied
    source = statuses.task_workflow_status(access, task)
    if source is None:
        raise TaskConflict("Для текущего этапа нет статуса.")
    if not target.is_active:
        raise TaskConflict("Архивный статус нельзя выбрать.")
    if source.pk == target.pk:
        return task

    rule = next((rule for rule in statuses.transition_rules(access, source.pk) if rule.to_status_id == target.pk), None)
    if rule is None:
        raise TaskConflict("Такой переход статуса не разрешён настройками организации.")
    if not statuses.can_execute_rule(access, task, context, rule):
        raise PermissionDenied
    conflict = _manual_stage_conflict(task, target.stage)
    if conflict is not None:
        raise TaskConflict(conflict)

    before = snapshot(task, ("status", "workflow_status_id"))
    task.workflow_status = target
    task.status = target.stage
    task.save(update_fields=["workflow_status", "status", "updated_at"])
    if task.status == Task.Status.CANCELLED and before["status"] != task.status:
        _cancel_distribution(task, access.membership)
    _audit_task(access.user, task, "status_changed", before=before, fields=tuple(before))
    return task


def _cancel_distribution(task, actor):
    task.distributions.filter(status=TaskDistribution.Status.PENDING).update(
        status=TaskDistribution.Status.CANCELLED, resolved_by=actor, resolved_at=timezone.now(),
    )
