from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404

from apps.access.services import organization_access
from apps.organizations.modules import TASKS, enabled

from .models import Task, TaskPriority

VIEW_CONFIGURATION = "tasks.workflow.view"
MANAGE_PRIORITIES = "tasks.priorities.manage"
LEGACY_PRIORITIES = frozenset(Task.Priority.values)
UNSET = object()


class PriorityConflict(Exception):
    """The priority change conflicts with catalog state."""


class PriorityValidation(Exception):
    """The task specifies an ambiguous priority."""


def can_view_configuration(access):
    if not enabled(access.organization, TASKS):
        return False
    return any(access.allows(permission) for permission in (
        VIEW_CONFIGURATION, MANAGE_PRIORITIES, "tasks.statuses.manage", "tasks.transitions.manage",
    ))


def available_priorities(organization):
    return organization.task_priorities.filter(is_active=True).order_by("sort_order", "id")


def list_priorities(*, user, org_id):
    access = organization_access(user=user, org_id=org_id, module=TASKS)
    if not can_view_configuration(access):
        raise PermissionDenied
    return access.allows(MANAGE_PRIORITIES), access.organization.task_priorities.order_by("sort_order", "id")


def _manager(user, org_id):
    access = organization_access(user=user, org_id=org_id, lock=True, module=TASKS)
    if not access.allows(MANAGE_PRIORITIES):
        raise PermissionDenied
    return access.organization


def _clear_current_default(organization):
    organization.task_priorities.filter(is_default=True).update(is_default=False)


@transaction.atomic
def create_priority(*, user, org_id, code, name, rank, color, sort_order=0, is_default=False):
    organization = _manager(user, org_id)
    if organization.task_priorities.filter(code=code).exists():
        raise PriorityConflict("Приоритет с этим кодом уже существует.")
    if is_default:
        _clear_current_default(organization)
    return organization.task_priorities.create(
        code=code, name=name, rank=rank, color=color,
        sort_order=sort_order, is_default=is_default,
    )


@transaction.atomic
def update_priority(*, user, org_id, priority_id, changes):
    organization = _manager(user, org_id)
    level = get_object_or_404(organization.task_priorities.select_for_update(), pk=priority_id)
    if not level.is_active:
        raise PriorityConflict("Архивный приоритет нельзя изменить.")
    fields = []
    if "is_default" in changes:
        is_default = changes["is_default"]
        if level.is_default and not is_default:
            raise PriorityConflict("Сначала выберите другой приоритет по умолчанию.")
        if is_default and not level.is_default:
            _clear_current_default(organization)
            level.is_default = True
            fields.append("is_default")
    for field in ("name", "rank", "color", "sort_order"):
        if field in changes:
            setattr(level, field, changes[field])
            fields.append(field)
    if fields:
        level.save(update_fields=[*fields, "updated_at"])
    return level


@transaction.atomic
def archive_priority(*, user, org_id, priority_id):
    organization = _manager(user, org_id)
    level = get_object_or_404(organization.task_priorities.select_for_update(), pk=priority_id)
    if not level.is_active:
        return
    if level.is_default:
        raise PriorityConflict("Сначала выберите другой приоритет по умолчанию.")
    if level.code in LEGACY_PRIORITIES:
        raise PriorityConflict("Стандартные уровни сохраняются для совместимости с прежним API.")
    if organization.tasks.filter(priority_level=level).exists():
        raise PriorityConflict("Этот приоритет уже используется задачами.")
    level.is_active = False
    level.save(update_fields=["is_active", "updated_at"])


def resolve_task_priority(organization, *, priority=UNSET, priority_id=UNSET):
    if priority is not UNSET and priority_id is not UNSET:
        raise PriorityValidation("Передайте только priority_id или прежнее поле priority.")
    levels = organization.task_priorities
    if priority_id is not UNSET:
        level = get_object_or_404(levels, pk=priority_id)
    elif priority is not UNSET:
        level = get_object_or_404(levels, code=priority)
    else:
        level = levels.filter(is_default=True, is_active=True).first()
        if level is None:
            raise PriorityConflict("В организации нет действующего приоритета по умолчанию.")
    if not level.is_active:
        raise PriorityConflict("Архивный приоритет нельзя выбрать для задачи.")
    return level


def legacy_priority_code(level):
    return level.code if level.code in LEGACY_PRIORITIES else Task.Priority.NORMAL


def priority_for_output(access, task):
    if task.priority_level_id is not None:
        return task.priority_level
    if not hasattr(access, "_legacy_priority_levels"):
        access._legacy_priority_levels = {
            level.code: level
            for level in access.organization.task_priorities.filter(code__in=LEGACY_PRIORITIES)
        }
    return access._legacy_priority_levels.get(task.priority)
