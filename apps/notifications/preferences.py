from datetime import date, datetime, timedelta, timezone

from django.db import transaction

from apps.access.services import organization_access

from .models import NotificationEvent, UserNotificationPreference
from .policy import PolicyValidation, _validated_list, policy_for

PREFERENCE_FIELDS = (
    "enabled_categories", "task_reminders", "calendar_reminders", "timezone", "quiet_start", "quiet_end",
)
UTC = timezone.utc


def preference_for(organization_id, user_id):
    return (
        UserNotificationPreference.objects.filter(organization_id=organization_id, user_id=user_id).first()
        or UserNotificationPreference(organization_id=organization_id, user_id=user_id)
    )


def preferences_for(organization_id, recipient_ids):
    return {
        preference.user_id: preference
        for preference in UserNotificationPreference.objects.filter(
            organization_id=organization_id, user_id__in=set(recipient_ids),
        )
    }


def preference_allows(preference, *, kind, category):
    if preference is None:
        return True
    if category not in preference.enabled_categories:
        return False
    if kind in {"task.reminder", "task.overdue"}:
        return preference.task_reminders
    if kind == "calendar.reminder":
        return preference.calendar_reminders
    return True


def _response(preference):
    return {
        "preferences": {field: getattr(preference, field) for field in PREFERENCE_FIELDS},
        "categories": [{"code": category, "label": category.label} for category in NotificationEvent.Category],
        "policy_enabled_categories": policy_for(preference.organization_id).enabled_categories,
    }


def get_preferences(*, user, org_id):
    access = organization_access(user=user, org_id=org_id)
    return _response(preference_for(access.organization.pk, user.pk))


@transaction.atomic
def update_preferences(*, user, org_id, changes):
    from apps.calendar_app.services import CalendarValidation, event_zone

    access = organization_access(user=user, org_id=org_id, lock=True)
    preference = preference_for(access.organization.pk, user.pk)
    if "enabled_categories" in changes:
        _validated_list(
            changes["enabled_categories"], NotificationEvent.Category.values,
            "Выберите доступные категории без повторов.",
        )
    if "timezone" in changes:
        try:
            event_zone(changes["timezone"])
        except CalendarValidation as exc:
            raise PolicyValidation(str(exc)) from None
    for field in PREFERENCE_FIELDS:
        if field in changes:
            setattr(preference, field, changes[field])

    start, end = preference.quiet_start, preference.quiet_end
    if (start is None) != (end is None) or (start is not None and start == end):
        raise PolicyValidation("Укажите два разных значения тихих часов или отключите оба.")
    if changes:
        if preference.pk is None:
            preference.save()
        else:
            preference.save(update_fields=[*changes, "updated_at"])
    return _response(preference)


def _wall_instant(day, clock, zone, *, end):
    wall = datetime.combine(day, clock, zone)
    requested = wall.replace(tzinfo=None)
    candidates = {wall.replace(fold=fold).astimezone(UTC) for fold in (0, 1)}
    exact = [instant for instant in candidates if instant.astimezone(zone).replace(tzinfo=None) == requested]
    if exact:
        # On a repeated clock interval, quiet hours include both folds.
        return max(exact) if end else min(exact)
    # A missing wall time moves forward by the actual offset change.
    return min(
        instant for instant in candidates
        if instant.astimezone(zone).replace(tzinfo=None) > requested
    )


def quiet_until(preference, now):
    if preference is None or preference.quiet_start is None or preference.quiet_end is None:
        return None
    from apps.calendar_app.services import event_zone

    zone = event_zone(preference.timezone)
    local_date = now.astimezone(zone).date()
    start, end = preference.quiet_start, preference.quiet_end
    crosses_midnight = end < start
    dates = [local_date]
    if crosses_midnight and local_date > date.min:
        dates.append(local_date - timedelta(days=1))

    active_ends = []
    now = now.astimezone(UTC)
    for start_date in dates:
        if crosses_midnight and start_date == date.max:
            continue
        end_date = start_date + timedelta(days=int(crosses_midnight))
        start_at = _wall_instant(start_date, start, zone, end=False)
        end_at = _wall_instant(end_date, end, zone, end=True)
        if start_at <= now < end_at:
            active_ends.append(end_at)
    return max(active_ends, default=None)
