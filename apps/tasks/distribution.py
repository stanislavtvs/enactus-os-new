from collections import defaultdict

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access
from apps.organizations.modules import TASKS, enabled
from apps.departments.models import DepartmentMembership
from apps.notifications.producers import emit, scoped_recipients
from apps.organizations.models import Organization

from .models import Task, TaskDistribution
from .services import (
    TaskConflict,
    _active_candidates,
    _audit_task,
    _has_pending_distribution,
    _managed_task,
    _task_is_writable,
    _task_rows,
    _task_visible,
    filter_enabled_tasks,
    task_scope_enabled,
)

ROUTE = "tasks.route"
VIEW = "tasks.distribution.view"
DISTRIBUTE = "tasks.distribute"


def can_route(access, task):
    return (
        _task_visible(access, task)
        and _task_is_writable(task)
        and access.organization.task_assignment_mode == Organization.TaskAssignmentMode.DEPARTMENT
        and task.status == Task.Status.PENDING
        and task.department_id is not None and not task.department.is_archived
        and task.assignee_id is None
        and not _has_pending_distribution(task)
        and access.allows(ROUTE, task.department)
    )


def _can_read(access, department):
    return access.allows(VIEW, department) or access.allows(DISTRIBUTE, department)


def can_open_distribution(access):
    if not enabled(access.organization, TASKS):
        return False
    return _can_read(access, None) or any(
        _can_read(access, department)
        for department in access.organization.departments.filter(is_archived=False)
    )


def can_distribute(access, distribution):
    return (
        task_scope_enabled(access.organization, distribution.task.project_id)
        and distribution.status == TaskDistribution.Status.PENDING
        and distribution.department_id is not None and not distribution.department.is_archived
        and _task_is_writable(distribution.task)
        and distribution.task.status == Task.Status.PENDING
        and distribution.task.assignee_id is None
        and access.allows(DISTRIBUTE, distribution.department)
    )


def _distribution_rows(access):
    rows = TaskDistribution.objects.filter(organization=access.organization)
    return filter_enabled_tasks(rows, access.organization, project_field="task__project_id").select_related(
        "task__project", "department", "requested_by__user", "resolved_by__user", "assignee__user",
    )


def distribution_history(*, access, task):
    rows = _distribution_rows(access).filter(task=task).order_by("-id")
    if _task_visible(access, task):
        return rows
    pending = pending_distribution_for_reader(access, task)
    return rows.filter(pk=pending.pk) if pending else rows.none()


def pending_distribution_for_reader(access, task):
    if not _task_is_writable(task) or task.status != Task.Status.PENDING or task.assignee_id is not None:
        return None
    item = _distribution_rows(access).filter(
        task=task, status=TaskDistribution.Status.PENDING, department__is_archived=False,
    ).first()
    if item is not None and _can_read(access, item.department):
        return item
    return None


def distribution_inbox(*, user, org_id):
    access = organization_access(user=user, org_id=org_id, module=TASKS)
    rows = _distribution_rows(access).filter(
        status=TaskDistribution.Status.PENDING, department__is_archived=False,
        task__is_archived=False, task__status=Task.Status.PENDING, task__assignee__isnull=True,
    ).exclude(task__project__is_archived=True).order_by("-id")
    return [item for item in rows if _can_read(access, item.department)]


@transaction.atomic
def route_task(*, user, org_id, task_id):
    access, task = _managed_task(user, org_id, task_id, ROUTE)
    if not can_route(access, task):
        raise TaskConflict("Для направления нужна свободная ожидающая задача с действующим подразделением.")
    distribution = task.distributions.create(
        organization=access.organization, department=task.department, requested_by=access.membership,
        mode=Organization.TaskAssignmentMode.DEPARTMENT,
    )
    _audit_task(user, task, "routed", metadata={"distribution_id": distribution.pk}, force=True)
    recipients = scoped_recipients(
        access.organization, (DISTRIBUTE,), lambda candidate: can_distribute(candidate, distribution),
    )
    emit(
        user, "task.distribution_requested", "task", task, recipients,
        key=f"task-distribution:{distribution.pk}:requested",
    )
    return distribution


def _pending_for_actor(access, task_id, *, lock=False):
    rows = _distribution_rows(access).filter(task_id=task_id, status=TaskDistribution.Status.PENDING)
    if lock:
        rows = rows.select_for_update(of=("self", "task"))
    distribution = get_object_or_404(rows)
    if not _can_read(access, distribution.department):
        raise Http404
    if not access.allows(DISTRIBUTE, distribution.department):
        raise PermissionDenied
    if not can_distribute(access, distribution):
        raise TaskConflict("Поручение больше не доступно для распределения.")
    return distribution


def _candidate_rows(access, distribution):
    children = defaultdict(list)
    for department in access.organization.departments.filter(is_archived=False):
        children[department.parent_id].append(department)

    eligible_departments = {distribution.department_id}
    pending = [distribution.department]
    while pending:
        department = pending.pop()
        pending.extend(children[department.pk])
        if access.allows(DISTRIBUTE, department):
            eligible_departments.add(department.pk)

    actual_memberships = DepartmentMembership.objects.filter(
        organization=access.organization, department_id__in=eligible_departments, is_active=True,
    ).values("organization_membership_id")
    candidates = _active_candidates(access, distribution.task).filter(
        pk__in=actual_memberships,
    ).exclude(collaborating_tasks__in=distribution.task.collaborators.filter(is_active=True)).exclude(
        pk=distribution.task.reviewer_id,
    )
    # Queue access alone must not let an allocator grant themselves ordinary task access.
    if not _task_visible(access, distribution.task):
        candidates = candidates.exclude(pk=access.membership.pk)
    return candidates.select_related("user").order_by("id")


def distribution_candidates(*, user, org_id, task_id):
    access = organization_access(user=user, org_id=org_id, module=TASKS)
    return _candidate_rows(access, _pending_for_actor(access, task_id))


@transaction.atomic
def distribute_task(*, user, org_id, task_id, assignee_id):
    access = organization_access(user=user, org_id=org_id, lock=True, module=TASKS)
    distribution = _pending_for_actor(access, task_id, lock=True)
    assignee = get_object_or_404(access.organization.memberships, pk=assignee_id)
    if not _candidate_rows(access, distribution).filter(pk=assignee.pk).exists():
        raise TaskConflict("Кандидат должен состоять в доступном подразделении и в команде проекта, если она нужна.")
    task = distribution.task
    before = {"assignee_id": task.assignee_id}
    task.assignee = assignee
    task.save(update_fields=["assignee", "updated_at"])
    distribution.assignee = assignee
    distribution.resolved_by = access.membership
    distribution.resolved_at = timezone.now()
    distribution.status = TaskDistribution.Status.ASSIGNED
    distribution.save(update_fields=["assignee", "resolved_by", "resolved_at", "status"])
    _audit_task(
        user, task, "assigned", before=before, fields=("assignee_id",),
        metadata={"distribution_id": distribution.pk, "mode": distribution.mode},
    )
    emit(
        user, "task.assigned", "task", task, {assignee.user_id, task.created_by.user_id},
        key=f"task-distribution:{distribution.pk}:assigned",
    )
    return distribution
