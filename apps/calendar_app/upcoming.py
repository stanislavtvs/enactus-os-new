from datetime import date, timedelta

from django.db.models import Prefetch, Q
from django.utils import timezone as django_timezone

from .models import CalendarEvent
from .recurrence import _dates, _occurrence, _schedule, _shift
from .recurrence_models import CalendarOccurrenceException
from .services import event_zone


def _future_end(event, end, now, viewer_day):
    if event.all_day:
        return end.astimezone(event_zone(event.timezone)).date() > viewer_day
    return end > now


def has_upcoming(event, now, viewer_day, exceptions=()):
    if event.status != CalendarEvent.Status.SCHEDULED:
        return False
    rule = getattr(event, "recurrence_rule", None)
    if rule is None:
        return _future_end(event, event.ends_at, now, viewer_day)

    schedule = _schedule(event)
    if event.all_day:
        start = _shift(viewer_day, -schedule.civil_days)
    else:
        start = _shift((now - schedule.duration).astimezone(schedule.zone).date(), -1)

    exceptions_by_date = {exception.occurrence_key: exception for exception in exceptions}
    for key, exception in exceptions_by_date.items():
        if exception.is_cancelled or exception.ends_at is None:
            continue
        if _future_end(event, exception.ends_at, now, viewer_day) and next(_dates(schedule.anchor, rule, key, key), None):
            return True
    # Jump straight to the relevant portion of the rule. Skip only persisted
    # exceptions or virtual dates whose end is already past.
    for key in _dates(schedule.anchor, rule, start, date.max):
        if key in exceptions_by_date and (
            exceptions_by_date[key].is_cancelled or exceptions_by_date[key].starts_at is not None
        ):
            continue
        occurrence = _occurrence(event, schedule, key)
        if occurrence is not None and _future_end(event, occurrence.ends_at, now, viewer_day):
            return True
    return False


def invitations_with_upcoming(rows, timezone_name):
    viewer_zone = event_zone(timezone_name)
    now = django_timezone.now()
    viewer_day = now.astimezone(viewer_zone).date()
    exceptions = CalendarOccurrenceException.objects.filter(
        Q(occurrence_key__gte=_shift(viewer_day, -370)) | Q(ends_at__gt=now - timedelta(days=2)),
    )
    rows = rows.prefetch_related(Prefetch(
        "event__occurrence_exceptions", queryset=exceptions, to_attr="_upcoming_exceptions",
    ))
    return [
        (row, has_upcoming(row.event, now, viewer_day, row.event._upcoming_exceptions)) for row in rows
    ]
