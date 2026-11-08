from collections import defaultdict

from django.db import transaction
from django.shortcuts import get_object_or_404

from .models import TaskDependency
from .services import (
    UPDATE, TaskConflict, _audit_task, _context, _managed_task, _related_audit_scope,
    _task_is_writable, visible_task,
)


def _rows(access, task):
    return task.dependencies.filter(organization=access.organization).select_related("predecessor").order_by("id")


def list_dependencies(*, access, task_id):
    task = visible_task(access=access, task_id=task_id)
    can_manage = _task_is_writable(task) and access.allows(UPDATE, _context(task.project, task.department))
    return task, can_manage, _rows(access, task)


def _creates_cycle(organization, task_id, predecessor_id):
    graph = defaultdict(list)
    for dependent, predecessor in TaskDependency.objects.filter(organization=organization).values_list(
        "task_id", "predecessor_id",
    ):
        graph[dependent].append(predecessor)

    visited = set()
    pending = [predecessor_id]
    while pending:
        current = pending.pop()
        if current == task_id:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(graph[current])
    return False


@transaction.atomic
def add_dependency(*, user, org_id, task_id, predecessor_id, kind):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    predecessor = visible_task(access=access, task_id=predecessor_id)
    if predecessor.pk == task.pk:
        raise TaskConflict("Задача не может зависеть от самой себя.")
    if _rows(access, task).filter(predecessor=predecessor).exists():
        raise TaskConflict("Такая зависимость уже существует.")
    if _creates_cycle(access.organization, task.pk, predecessor.pk):
        raise TaskConflict("Такая зависимость создаёт цикл.")
    dependency = task.dependencies.create(organization=access.organization, predecessor=predecessor, kind=kind)
    _audit_dependency(user, task, dependency, "dependency_added")
    return dependency


@transaction.atomic
def remove_dependency(*, user, org_id, task_id, dependency_id):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    dependency = get_object_or_404(_rows(access, task), pk=dependency_id)
    _audit_dependency(user, task, dependency, "dependency_removed")
    dependency.delete()


def _audit_dependency(user, task, dependency, action):
    _audit_task(
        user, task, action, force=True,
        metadata={
            **_related_audit_scope(dependency.predecessor), "dependency_id": dependency.pk,
            "predecessor_id": dependency.predecessor_id, "kind": dependency.kind,
        },
    )
