from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from itertools import islice
from math import gcd
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import Http404
from django.utils import timezone as django_timezone

from apps.access.services import organization_access
from apps.notifications.producers import calendar_recipients, emit

from .models import CalendarEvent
from .recurrence_models import CalendarOccurrenceException, CalendarRecurrenceRule
from .services import (
    CANCEL, UPDATE, CalendarConflict, CalendarValidation, _allows, _can_read_event,
    _civil_day_start, _date_bounds, _validate_interval, event_item, event_zone, visible_event,
)

UTC = timezone.utc
FREQUENCY = CalendarRecurrenceRule.Frequency
MAX_OCCURRENCES_PER_SERIES = 1_000


@dataclass(frozen=True)
class Occurrence:
    key: date
    starts_at: datetime
    ends_at: datetime
    original_starts_at: datetime
    original_ends_at: datetime
    status: str
    exception: object | None = None

    @property
    def version(self):
        return self.exception.version if self.exception is not None else 0

    @property
    def rsvp_revision(self):
        return self.exception.rsvp_revision if self.exception is not None else 0


def _rule(event):
    return getattr(event, "recurrence_rule", None)


def rule_data(rule):
    if rule is None:
        return None
    return {
        "frequency": rule.frequency,
        "interval": rule.interval,
        "weekdays": rule.weekdays,
        "until": rule.until,
        "count": rule.count,
        "stopped_from": rule.stopped_from,
    }


def _clamped_date(ordinal):
    return date.fromordinal(max(date.min.toordinal(), min(date.max.toordinal(), ordinal)))


def _shift(day, days):
    return _clamped_date(day.toordinal() + days)


def _month_number(day):
    return day.year * 12 + day.month - 1


@lru_cache(maxsize=128)
def _valid_month_prefix(start_modulo, day, interval):
    period = 4800 // gcd(4800, interval)
    result = [0]
    for index in range(period):
        year, month = divmod((start_modulo + index * interval) % 4800, 12)
        result.append(result[-1] + (day <= monthrange(2000 + year, month + 1)[1]))
    return result


def _valid_months_before(anchor_month, day, interval, index):
    if day <= 28:
        return index
    prefix = _valid_month_prefix(anchor_month % 4800, day, interval)
    periods, remainder = divmod(index, len(prefix) - 1)
    return periods * prefix[-1] + prefix[remainder]


def _monthly_dates(anchor, start, end, interval, count):
    anchor_month = _month_number(anchor)
    first = max(0, (_month_number(start) - anchor_month + interval - 1) // interval)
    last = (_month_number(end) - anchor_month) // interval
    ordinal = _valid_months_before(anchor_month, anchor.day, interval, first) if count is not None else 0
    for index in range(first, last + 1):
        if count is not None and ordinal >= count:
            break
        year, month = divmod(anchor_month + index * interval, 12)
        if anchor.day > monthrange(year, month + 1)[1]:
            continue
        ordinal += 1
        candidate = date(year, month + 1, anchor.day)
        if start <= candidate <= end:
            yield candidate


def _weekly_dates(anchor, start, end, interval, weekdays, count):
    week_start = anchor.toordinal() - anchor.isoweekday() + 1
    stride = 7 * interval
    first = max(0, (start.toordinal() - week_start) // stride)
    last = (end.toordinal() - week_start) // stride
    ordinal = 0
    if first > 0:
        ordinal = sum(day >= anchor.isoweekday() for day in weekdays) + (first - 1) * len(weekdays)
    for index in range(first, last + 1):
        for weekday in weekdays:
            candidate = week_start + index * stride + weekday - 1
            if candidate < anchor.toordinal():
                continue
            ordinal += 1
            if count is not None and ordinal > count:
                return
            if start.toordinal() <= candidate <= end.toordinal():
                yield date.fromordinal(candidate)


def _dates(anchor, rule, start, end):
    start = max(start, anchor)
    if rule.until is not None:
        end = min(end, rule.until)
    if rule.stopped_from is not None:
        if rule.stopped_from <= start:
            return
        end = min(end, rule.stopped_from - timedelta(days=1))
    if start > end:
        return
    if rule.frequency == FREQUENCY.DAILY:
        first = ((start - anchor).days + rule.interval - 1) // rule.interval
        last = (end - anchor).days // rule.interval
        if rule.count is not None:
            last = min(last, rule.count - 1)
        for index in range(first, last + 1):
            yield anchor + timedelta(days=index * rule.interval)
    elif rule.frequency == FREQUENCY.WEEKLY:
        yield from _weekly_dates(
            anchor, start, end, rule.interval, rule.weekdays or [anchor.isoweekday()], rule.count,
        )
    else:
        month_interval = rule.interval * (12 if rule.frequency == FREQUENCY.YEARLY else 1)
        yield from _monthly_dates(anchor, start, end, month_interval, rule.count)


def _local_start(day, clock, zone):
    wall_time = datetime.combine(day, clock, zone).replace(fold=0)
    requested = wall_time.replace(tzinfo=None)
    earlier = wall_time.astimezone(UTC)
    if earlier.astimezone(zone).replace(tzinfo=None) == requested:
        return earlier
    # For a gap, use the first round-trip that lands after the requested wall
    # time on the same civil date. A skipped civil date has no occurrence.
    other = wall_time.replace(fold=1).astimezone(UTC)
    valid = [
        instant for instant in (earlier, other)
        if instant.astimezone(zone).date() == day
        and instant.astimezone(zone).replace(tzinfo=None) > requested
    ]
    return min(valid) if valid else None


def _schedule(event):
    zone = event_zone(event.timezone)
    local_start = event.starts_at.astimezone(zone)
    local_end = event.ends_at.astimezone(zone)
    return SimpleNamespace(
        zone=zone, anchor=local_start.date(), clock=local_start.time(),
        duration=event.ends_at.astimezone(UTC) - event.starts_at.astimezone(UTC),
        civil_days=(local_end.date() - local_start.date()).days,
    )


def _base_times(event, schedule, day):
    if day == schedule.anchor:
        return event.starts_at, event.ends_at
    try:
        if event.all_day:
            starts_at = _civil_day_start(day, schedule.zone)
            ends_at = _civil_day_start(day + timedelta(days=schedule.civil_days), schedule.zone)
        else:
            starts_at = _local_start(day, schedule.clock, schedule.zone)
            if starts_at is None:
                return None
            ends_at = starts_at + schedule.duration
    except (OverflowError, ValueError):
        return None
    return (starts_at, ends_at) if ends_at > starts_at else None


def _status(event, starts_at, exception):
    if event.status == CalendarEvent.Status.ARCHIVED:
        return CalendarEvent.Status.ARCHIVED
    if exception is not None and exception.is_cancelled:
        return CalendarEvent.Status.CANCELLED
    if event.cancelled_at is not None and starts_at >= event.cancelled_at:
        return CalendarEvent.Status.CANCELLED
    return CalendarEvent.Status.SCHEDULED


def _occurrence(event, schedule, day, exception=None):
    original = _base_times(event, schedule, day)
    if original is None:
        return None
    starts_at, ends_at = original
    if exception is not None and exception.starts_at is not None:
        starts_at, ends_at = exception.starts_at, exception.ends_at
    return Occurrence(day, starts_at, ends_at, *original, _status(event, starts_at, exception), exception)


def _overlaps(event, occurrence, schedule, start, end, bounds):
    if event.all_day:
        return (
            occurrence.starts_at.astimezone(schedule.zone).date() < end
            and occurrence.ends_at.astimezone(schedule.zone).date() > start
        )
    return occurrence.starts_at < bounds[1] and occurrence.ends_at > bounds[0]


def expand_event_occurrences(
    event, *, start, end, timezone_name, include_cancelled=False, exceptions=(), bounds=None,
):
    if bounds is None:
        bounds = _date_bounds(start, end, timezone_name)
    rule = _rule(event)
    if rule is None:
        return []
    schedule = _schedule(event)
    if event.all_day:
        scan_start = _shift(start, -schedule.civil_days)
        scan_end = _shift(end, -1)
    else:
        try:
            scan_start = _shift((bounds[0] - schedule.duration).astimezone(schedule.zone).date(), -1)
        except (OverflowError, ValueError):
            scan_start = date.min
        scan_end = _shift(bounds[1].astimezone(schedule.zone).date(), 1)

    exceptions_by_day = {exception.occurrence_key: exception for exception in exceptions}
    days = set(islice(
        _dates(schedule.anchor, rule, scan_start, scan_end), MAX_OCCURRENCES_PER_SERIES + 1,
    ))
    days.update(
        key for key in exceptions_by_day
        if next(_dates(schedule.anchor, rule, key, key), None) is not None
    )
    if len(days) > MAX_OCCURRENCES_PER_SERIES:
        raise CalendarValidation("Слишком много повторений серии в диапазоне. Сузьте диапазон.")
    result = []
    for day in sorted(days):
        occurrence = _occurrence(event, schedule, day, exceptions_by_day.get(day))
        if occurrence is None or (occurrence.status == CalendarEvent.Status.CANCELLED and not include_cancelled):
            continue
        if _overlaps(event, occurrence, schedule, start, end, bounds):
            result.append(occurrence)
    return sorted(result, key=lambda occurrence: (occurrence.starts_at, occurrence.key))


def _moved_overlap(bounds):
    return Q(starts_at__lt=bounds[3], ends_at__gt=bounds[2])


def _range_exception_filter(start, end, bounds):
    # Base durations are capped at 366 days; the additional days cover
    # viewer/event civil-date differences and a forward timezone gap.
    return (
        Q(occurrence_key__gte=_shift(start, -369), occurrence_key__lte=_shift(end, 3))
        | _moved_overlap(bounds)
    )


def prepare_occurrence_range(rows, *, start, end, bounds):
    moved = CalendarOccurrenceException.objects.filter(
        organization_id=OuterRef("organization_id"), event_id=OuterRef("pk"),
    ).filter(_moved_overlap(bounds))
    exceptions = CalendarOccurrenceException.objects.filter(
        _range_exception_filter(start, end, bounds),
    ).order_by("occurrence_key")[:MAX_OCCURRENCES_PER_SERIES + 1]
    earliest_key = _shift(start, -369)
    base_candidate = (
        Q(starts_at__lt=bounds[3])
        & (Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=earliest_key))
        & (Q(recurrence_rule__stopped_from__isnull=True) | Q(recurrence_rule__stopped_from__gt=earliest_key))
    )
    return rows.filter(recurrence_rule__isnull=False).filter(
        base_candidate | Exists(moved),
    ).prefetch_related(Prefetch("occurrence_exceptions", queryset=exceptions, to_attr="_range_exceptions"))


def list_occurrences(*, event, start, end, timezone_name, include_cancelled=False):
    bounds = _date_bounds(start, end, timezone_name)
    exceptions = event.occurrence_exceptions.filter(
        _range_exception_filter(start, end, bounds),
    ).order_by("occurrence_key")[:MAX_OCCURRENCES_PER_SERIES + 1]
    return expand_event_occurrences(
        event, start=start, end=end, timezone_name=timezone_name,
        include_cancelled=include_cancelled, exceptions=exceptions, bounds=bounds,
    )


def resolve_occurrence(event, key):
    rule = _rule(event)
    if rule is None:
        raise Http404
    schedule = _schedule(event)
    if next(_dates(schedule.anchor, rule, key, key), None) is None:
        raise Http404
    exceptions = getattr(event, "occurrence_exceptions", None)
    exception = exceptions.filter(occurrence_key=key).first() if exceptions is not None else None
    occurrence = _occurrence(event, schedule, key, exception)
    if occurrence is None:
        raise Http404
    return occurrence


def occurrence_item(access, event, occurrence):
    data = event_item(access, event)
    data.update({
        "series_id": event.pk,
        "occurrence_key": occurrence.key,
        "original_starts_at": occurrence.original_starts_at,
        "original_ends_at": occurrence.original_ends_at,
        "is_recurring": True,
        "is_exception": occurrence.exception is not None,
        "occurrence_version": occurrence.version,
        "occurrence_rsvp_revision": occurrence.rsvp_revision,
        "recurrence": rule_data(_rule(event)),
        "starts_at": occurrence.starts_at,
        "ends_at": occurrence.ends_at,
        "status": occurrence.status,
        "status_label": CalendarEvent.Status(occurrence.status).label,
    })
    exception = occurrence.exception
    if exception is not None:
        for field in ("title", "location", "online_url"):
            value = getattr(exception, field)
            if value is not None:
                data[field] = value if field == "title" else value or None
        if exception.description is not None and _can_read_event(access, event):
            data["description"] = exception.description
        data["updated_at"] = max(event.updated_at, exception.updated_at)
    if occurrence.status != CalendarEvent.Status.SCHEDULED:
        data["can_edit"] = False
        data["can_cancel"] = False
    return data


def _normalized_rule(event, payload):
    if payload["until"] is not None and payload["count"] is not None:
        raise CalendarValidation("Укажите дату окончания или число повторений.")
    schedule = _schedule(event)
    anchor = schedule.anchor
    if schedule.civil_days > 366 or schedule.duration > timedelta(days=366):
        raise CalendarValidation("Одно повторение серии может длиться не больше 366 дней.")
    if payload["until"] is not None and payload["until"] < anchor:
        raise CalendarValidation("Дата окончания повторения не может быть раньше начала.")
    weekdays = payload["weekdays"]
    if payload["frequency"] == FREQUENCY.WEEKLY:
        weekdays = sorted(set(weekdays or [anchor.isoweekday()]))
        if anchor.isoweekday() not in weekdays:
            raise CalendarValidation("Среди дней недели должен быть день начала серии.")
    elif weekdays:
        raise CalendarValidation("Дни недели можно выбирать только для еженедельного повторения.")
    return {**payload, "weekdays": weekdays, "timezone": event.timezone}


def install_rule(event, payload):
    fields = _normalized_rule(event, payload)
    rule, _ = CalendarRecurrenceRule.objects.update_or_create(
        event=event, defaults={**fields, "organization": event.organization},
    )
    event.recurrence_rule = rule
    return rule


def _history_dates(event):
    keys = set(event.occurrence_exceptions.values_list("occurrence_key", flat=True))
    keys.update(event.occurrence_response_history.order_by().values_list("occurrence_key", flat=True).distinct())
    return keys


def validate_series_schedule_change(event, changes):
    rule = _rule(event)
    schedule_fields = ("starts_at", "ends_at", "timezone", "all_day")
    if rule is None or not any(
        field in changes and changes[field] != getattr(event, field) for field in schedule_fields
    ):
        return
    if _history_dates(event):
        raise CalendarConflict("У серии есть исключения или история повторений. Измените отдельную встречу.")
    proposed = SimpleNamespace(**{field: changes.get(field, getattr(event, field)) for field in schedule_fields})
    _normalized_rule(proposed, {key: value for key, value in rule_data(rule).items() if key != "stopped_from"})


def sync_recurrence_timezone(event):
    rule = _rule(event)
    if rule is not None and rule.timezone != event.timezone:
        rule.timezone = event.timezone
        rule.save(update_fields=["timezone", "updated_at"])


def can_edit_rule(access, event):
    return (
        event.status == CalendarEvent.Status.SCHEDULED
        and _can_read_event(access, event)
        and _allows(access, UPDATE, event.project, event.department)
    )


def recurrence_detail(*, access, event_id):
    event = visible_event(access=access, event_id=event_id, allow_invitation=True)
    return {"recurrence": rule_data(_rule(event)), "can_edit": can_edit_rule(access, event)}


def _editable_event(user, org_id, event_id, action=UPDATE):
    access = organization_access(user=user, org_id=org_id, lock=True)
    event = visible_event(access=access, event_id=event_id, lock=True)
    if not _allows(access, action, event.project, event.department):
        raise PermissionDenied
    if event.status != CalendarEvent.Status.SCHEDULED:
        raise CalendarConflict("Изменить можно только запланированную серию.")
    return access, event


@transaction.atomic
def set_recurrence(*, user, org_id, event_id, payload):
    access, event = _editable_event(user, org_id, event_id)
    existing = _rule(event)
    history = _history_dates(event)
    if payload is None:
        if existing is not None:
            if history or event.invitations.filter(response__isnull=False).exists():
                raise CalendarConflict("Нельзя убрать повторение: у серии есть исключения или ответы.")
            existing.delete()
            event.recurrence_rule = None
            _bump_event_revision(event)
            _notify_series_change(user, event)
        return {"recurrence": None, "can_edit": True}

    fields = _normalized_rule(event, payload)
    proposed = SimpleNamespace(**fields, stopped_from=existing.stopped_from if existing else None)
    anchor = _schedule(event).anchor
    if any(next(_dates(anchor, proposed, key, key), None) is None for key in history):
        raise CalendarConflict("Новое правило исключает даты с сохранённой историей.")
    changed = existing is None or any(getattr(existing, field) != value for field, value in fields.items())
    rule = install_rule(event, payload) if changed else existing
    if changed:
        _bump_event_revision(event)
        _notify_series_change(user, event)
    return {"recurrence": rule_data(rule), "can_edit": can_edit_rule(access, event)}


def _bump_event_revision(event):
    event.rsvp_revision += 1
    event.save(update_fields=["rsvp_revision", "updated_at"])


def _notify_series_change(user, event):
    emit(user, "calendar.rescheduled", "calendar_event", event, calendar_recipients(event),
         key=f"calendar.schedule:{event.pk}:{event.rsvp_revision}")


def _check_occurrence_version(occurrence, expected_version):
    if occurrence.version != expected_version:
        raise CalendarConflict("Расписание встречи уже изменилось. Обновите карточку.")
    if occurrence.status != CalendarEvent.Status.SCHEDULED:
        raise CalendarConflict("Изменить можно только запланированную встречу.")


def _new_exception(event, key):
    return CalendarOccurrenceException(
        organization_id=event.organization_id, event=event, occurrence_key=key,
    )


@transaction.atomic
def update_occurrence(*, user, org_id, event_id, key, expected_version, changes):
    access, event = _editable_event(user, org_id, event_id)
    occurrence = resolve_occurrence(event, key)
    _check_occurrence_version(occurrence, expected_version)
    exception = occurrence.exception or _new_exception(event, key)
    fields = []
    for field in ("title", "description", "location", "online_url"):
        if field in changes and changes[field] != getattr(exception, field):
            setattr(exception, field, changes[field])
            fields.append(field)

    if "starts_at" in changes or "ends_at" in changes:
        starts_at = changes.get("starts_at", occurrence.starts_at) or occurrence.original_starts_at
        ends_at = changes.get("ends_at", occurrence.ends_at) or occurrence.original_ends_at
        _validate_interval(starts_at, ends_at, event.timezone, event.all_day)
        if (starts_at, ends_at) != (occurrence.starts_at, occurrence.ends_at):
            original = (occurrence.original_starts_at, occurrence.original_ends_at)
            exception.starts_at, exception.ends_at = (None, None) if (starts_at, ends_at) == original else (starts_at, ends_at)
            exception.rsvp_revision += 1
            fields.extend(["starts_at", "ends_at", "rsvp_revision"])

    if fields:
        if exception.pk is None:
            exception.save()
        else:
            exception.version += 1
            exception.save(update_fields=[*fields, "version", "updated_at"])
        occurrence = _occurrence(event, _schedule(event), key, exception)
        if "rsvp_revision" in fields:
            emit(user, "calendar.occurrence_rescheduled", "calendar_event", event, calendar_recipients(event),
                 key=f"calendar.move:{event.pk}:{key}:{exception.rsvp_revision}", data={"occurrence_key": key})
    return occurrence_item(access, event, occurrence)


@transaction.atomic
def cancel_occurrence(*, user, org_id, event_id, key, expected_version):
    access, event = _editable_event(user, org_id, event_id, CANCEL)
    occurrence = resolve_occurrence(event, key)
    _check_occurrence_version(occurrence, expected_version)
    exception = occurrence.exception or _new_exception(event, key)
    exception.is_cancelled = True
    exception.cancelled_at = django_timezone.now()
    if exception.pk is None:
        exception.save()
    else:
        exception.version += 1
        exception.save(update_fields=["is_cancelled", "cancelled_at", "version", "updated_at"])
    emit(user, "calendar.occurrence_cancelled", "calendar_event", event, calendar_recipients(event),
         key=f"calendar.cancel-date:{event.pk}:{key}", data={"occurrence_key": key})
    return occurrence_item(access, event, _occurrence(event, _schedule(event), key, exception))


@transaction.atomic
def stop_recurrence(*, user, org_id, event_id, from_date):
    access, event = _editable_event(user, org_id, event_id, CANCEL)
    rule = _rule(event)
    if rule is None:
        raise Http404
    if rule.stopped_from == from_date:
        return {"recurrence": rule_data(rule), "can_edit": can_edit_rule(access, event)}
    now = django_timezone.now()
    today = now.astimezone(event_zone(event.timezone)).date()
    if from_date < today:
        raise CalendarValidation("Остановить можно только будущие повторения.")
    if from_date == today:
        try:
            today_occurrence = resolve_occurrence(event, from_date)
        except Http404:
            today_occurrence = None
        if today_occurrence is not None and today_occurrence.starts_at < now:
            raise CalendarValidation("Выбранная дата включает уже начавшуюся встречу.")
    if rule.stopped_from is not None and from_date > rule.stopped_from:
        raise CalendarConflict("Остановка не может восстановить уже исключённые повторения.")
    if any(key >= from_date for key in _history_dates(event)):
        raise CalendarConflict("После выбранной даты есть исключения или история. Выберите более позднюю дату.")
    rule.stopped_from = from_date
    rule.save(update_fields=["stopped_from", "updated_at"])
    emit(user, "calendar.series_stopped", "calendar_event", event, calendar_recipients(event),
         key=f"calendar.stop:{event.pk}:{from_date}")
    return {"recurrence": rule_data(rule), "can_edit": can_edit_rule(access, event)}
