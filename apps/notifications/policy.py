from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.access.services import organization_access

from .models import NotificationEvent, OrganizationNotificationPolicy

MANAGE = "notifications.manage"
TASK_OFFSETS_MINUTES = (1440, 180, 60, 0)
CALENDAR_OFFSETS_MINUTES = (1440, 60, 15)
REMINDER_TITLES = {
    "task.reminder": "Приближается дедлайн задачи",
    "task.overdue": "Задача просрочена",
    "calendar.reminder": "Предстоящее событие",
}
POLICY_FIELDS = (
    "enabled_categories", "disabled_kinds", "task_offsets_minutes", "calendar_offsets_minutes",
    "task_overdue", "include_collaborators", "calendar_informational", "all_day_reminder_time",
)


class PolicyValidation(Exception):
    """The notification configuration contains unsupported values."""


def policy_for(organization_id):
    return (
        OrganizationNotificationPolicy.objects.filter(organization_id=organization_id).first()
        or OrganizationNotificationPolicy(organization_id=organization_id)
    )


def policy_allows(policy, *, kind, category):
    return category in policy.enabled_categories and kind not in policy.disabled_kinds


def _kind_labels():
    from .visibility import TITLES

    return {**TITLES, **REMINDER_TITLES}


def _options():
    from .services import CATEGORIES

    return {
        "categories": [{"code": category, "label": category.label} for category in NotificationEvent.Category],
        "kinds": [
            {"code": kind, "category": CATEGORIES[kind.split(".", 1)[0]], "label": label}
            for kind, label in _kind_labels().items()
        ],
        "task_offset_options": TASK_OFFSETS_MINUTES,
        "calendar_offset_options": CALENDAR_OFFSETS_MINUTES,
    }


def _response(policy):
    return {
        "policy": {field: getattr(policy, field) for field in POLICY_FIELDS},
        **_options(),
    }


def _manager(user, org_id, *, lock=False):
    access = organization_access(user=user, org_id=org_id, lock=lock)
    if not access.allows(MANAGE):
        raise PermissionDenied
    return access.organization


def get_policy(*, user, org_id):
    organization = _manager(user, org_id)
    return _response(policy_for(organization.pk))


def _validated_list(values, allowed, message):
    if len(set(values)) != len(values) or any(value not in allowed for value in values):
        raise PolicyValidation(message)
    return values


@transaction.atomic
def update_policy(*, user, org_id, changes):
    organization = _manager(user, org_id, lock=True)
    policy = policy_for(organization.pk)
    changes = dict(changes)
    for field, allowed, message in (
        ("enabled_categories", NotificationEvent.Category.values, "Выберите доступные категории без повторов."),
        ("disabled_kinds", _kind_labels(), "Выберите известные виды уведомлений без повторов."),
        ("task_offsets_minutes", TASK_OFFSETS_MINUTES, "Выберите допустимые интервалы задач без повторов."),
        ("calendar_offsets_minutes", CALENDAR_OFFSETS_MINUTES, "Выберите допустимые интервалы событий без повторов."),
    ):
        if field in changes:
            changes[field] = _validated_list(changes[field], allowed, message)
    for field in POLICY_FIELDS:
        if field in changes:
            setattr(policy, field, changes[field])
    if changes:
        if policy.pk is None:
            policy.save()
        else:
            policy.save(update_fields=[*changes, "updated_at"])
    return _response(policy)
