from collections import defaultdict

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404

from apps.access.policy import ORGANIZATION_ONLY, Permission
from apps.access.services import organization_access
from apps.organizations.modules import TASKS

from .models import Task, TaskStatus
from .priorities import can_view_configuration

MANAGE_STATUSES = "tasks.statuses.manage"
MANAGE_TRANSITIONS = "tasks.transitions.manage"
CHANGE_STATUS = "tasks.change_status"
ASSIGNEE_EXECUTE = "tasks.status.execute"

MANUAL_STAGE_TRANSITIONS = {
    Task.Status.PENDING: (Task.Status.IN_PROGRESS, Task.Status.CANCELLED),
    Task.Status.IN_PROGRESS: (Task.Status.CANCELLED,),
    Task.Status.REVIEW: (Task.Status.CANCELLED,),
}


class StatusConflict(Exception):
    """The requested status operation conflicts with workflow state."""


class StatusValidation(Exception):
    """The status or transition configuration is invalid."""


def available_statuses(organization):
    return organization.task_statuses.filter(is_active=True).order_by("sort_order", "id")


def list_statuses(*, user, org_id):
    access = organization_access(user=user, org_id=org_id, module=TASKS)
    if not can_view_configuration(access):
        raise PermissionDenied
    return access.allows(MANAGE_STATUSES), access.organization.task_statuses.order_by("sort_order", "id")


def list_transitions(*, user, org_id):
    access = organization_access(user=user, org_id=org_id, module=TASKS)
    if not can_view_configuration(access):
        raise PermissionDenied
    return access.allows(MANAGE_TRANSITIONS), access.organization.task_status_transitions.order_by("id")


def _manager(user, org_id, action):
    access = organization_access(user=user, org_id=org_id, lock=True, module=TASKS)
    if not access.allows(action):
        raise PermissionDenied
    return access.organization


def _clear_default(organization, stage):
    organization.task_statuses.filter(stage=stage, is_default=True).update(is_default=False)


@transaction.atomic
def create_status(*, user, org_id, code, name, stage, color, sort_order=0, is_default=False):
    organization = _manager(user, org_id, MANAGE_STATUSES)
    if organization.task_statuses.filter(code=code).exists():
        raise StatusConflict("Статус с таким кодом уже существует.")
    if is_default:
        _clear_default(organization, stage)
    return organization.task_statuses.create(
        code=code, name=name, stage=stage, color=color, sort_order=sort_order, is_default=is_default,
    )


@transaction.atomic
def update_status(*, user, org_id, status_id, changes):
    organization = _manager(user, org_id, MANAGE_STATUSES)
    status = get_object_or_404(organization.task_statuses.select_for_update(), pk=status_id)
    if not status.is_active:
        raise StatusConflict("Архивный статус нельзя изменить.")
    fields = []
    if "is_default" in changes:
        is_default = changes["is_default"]
        if status.is_default and not is_default:
            raise StatusConflict("Сначала выберите другой статус по умолчанию для этого этапа.")
        if is_default and not status.is_default:
            _clear_default(organization, status.stage)
            status.is_default = True
            fields.append("is_default")
    for field in ("name", "color", "sort_order"):
        if field in changes:
            setattr(status, field, changes[field])
            fields.append(field)
    if fields:
        status.save(update_fields=[*fields, "updated_at"])
    return status


@transaction.atomic
def archive_status(*, user, org_id, status_id):
    organization = _manager(user, org_id, MANAGE_STATUSES)
    status = get_object_or_404(organization.task_statuses.select_for_update(), pk=status_id)
    if not status.is_active:
        return
    if status.is_default:
        raise StatusConflict("Сначала выберите другой статус по умолчанию для этого этапа.")
    status.is_active = False
    status.save(update_fields=["is_active", "updated_at"])


def stage_transition_allowed(source_stage, target_stage):
    return source_stage == target_stage or target_stage in MANUAL_STAGE_TRANSITIONS.get(source_stage, ())


def _validate_permissions(required_permissions):
    if len(set(required_permissions)) != len(required_permissions) or any(
        action not in Permission.values for action in required_permissions
    ):
        raise StatusValidation("Укажите действующие разрешения без повторов.")


@transaction.atomic
def create_transition(
    *, user, org_id, from_status_id, to_status_id, is_enabled=True, allow_assignee=False,
    required_permissions=(),
):
    organization = _manager(user, org_id, MANAGE_TRANSITIONS)
    source = get_object_or_404(organization.task_statuses, pk=from_status_id)
    target = get_object_or_404(organization.task_statuses, pk=to_status_id)
    if source.pk == target.pk:
        raise StatusConflict("Исходный и целевой статусы должны различаться.")
    if not target.is_active:
        raise StatusConflict("Нельзя выбрать архивный целевой статус.")
    if not stage_transition_allowed(source.stage, target.stage):
        raise StatusConflict("Этот переход между системными этапами выполняется только рабочим процессом.")
    _validate_permissions(required_permissions)
    rules = organization.task_status_transitions
    if rules.filter(from_status=source, to_status=target).exists():
        raise StatusConflict("Такой переход уже настроен.")
    return rules.create(
        from_status=source, to_status=target, is_enabled=is_enabled, allow_assignee=allow_assignee,
        required_permissions=list(required_permissions),
    )


@transaction.atomic
def update_transition(*, user, org_id, transition_id, changes):
    organization = _manager(user, org_id, MANAGE_TRANSITIONS)
    rule = get_object_or_404(
        organization.task_status_transitions.select_for_update(of=("self",)).select_related("to_status"),
        pk=transition_id,
    )
    fields = []
    if changes.get("is_enabled") and not rule.to_status.is_active:
        raise StatusConflict("Нельзя включить переход в архивный статус.")
    if "required_permissions" in changes:
        _validate_permissions(changes["required_permissions"])
    for field in ("is_enabled", "allow_assignee", "required_permissions"):
        if field in changes:
            setattr(rule, field, changes[field])
            fields.append(field)
    if fields:
        rule.save(update_fields=[*fields, "updated_at"])
    return rule


def default_statuses(access):
    if not hasattr(access, "_task_stage_defaults"):
        access._task_stage_defaults = {
            status.stage: status
            for status in access.organization.task_statuses.filter(is_active=True, is_default=True)
        }
    return access._task_stage_defaults


def task_workflow_status(access, task):
    if task.workflow_status_id is not None:
        return task.workflow_status
    return default_statuses(access).get(task.status)


def can_attempt_transition(access, task, department):
    return access.allows(CHANGE_STATUS, department) or (
        access.membership is not None
        and task.assignee_id == access.membership.pk
        and access.allows(ASSIGNEE_EXECUTE, department)
    )


def can_execute_rule(access, task, department, rule):
    if not access.allows(CHANGE_STATUS, department) and not (
        rule.allow_assignee
        and access.membership is not None
        and task.assignee_id == access.membership.pk
        and access.allows(ASSIGNEE_EXECUTE, department)
    ):
        return False
    return all(
        access.allows(action, None if action in ORGANIZATION_ONLY else department)
        for action in rule.required_permissions
    )


def transition_rules(access, source_id):
    if not hasattr(access, "_task_transition_rules"):
        rules = defaultdict(list)
        rows = access.organization.task_status_transitions.filter(
            is_enabled=True, to_status__is_active=True,
        ).select_related("to_status").order_by("to_status__sort_order", "to_status_id")
        for rule in rows:
            rules[rule.from_status_id].append(rule)
        access._task_transition_rules = rules
    return access._task_transition_rules[source_id]
