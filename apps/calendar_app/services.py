from datetime import datetime, time, timedelta, timezone as datetime_timezone
from functools import cache, lru_cache
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from apps.access.services import organization_access
from apps.projects.models import ProjectMembership
from apps.projects.services import _can_view as can_view_project
from apps.projects.services import _context as project_context
from apps.projects.services import visible_projects
from apps.tasks.services import can_open_tasks, visible_task_deadlines
from apps.tasks.statuses import task_workflow_status

from .models import CalendarEvent

READ = "calendar.read"
CREATE = "calendar.create"
UPDATE = "calendar.update"
CANCEL = "calendar.cancel"
ARCHIVE = "calendar.archive"
MANAGE_SCOPE = "calendar.manage_scope"
MANAGE_INVITATIONS = "calendar.invite.manage"
MAX_RANGE_DAYS = 42
MAX_CALENDAR_ITEMS = 2000
# Civil all-day dates can differ from viewer dates by more than 24 hours.
ALL_DAY_RANGE_MARGIN = timedelta(days=2)


class CalendarValidation(Exception):
    """The requested date range or event fields are invalid."""


class CalendarConflict(Exception):
    """The event or a linked object has an incompatible state."""


@cache
def _iana_zone_names():
    return available_timezones() - {"Factory", "localtime", "posixrules"}


@lru_cache(maxsize=128)
def event_zone(name):
    try:
        if name not in _iana_zone_names():
            raise ValueError
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarValidation("Укажите действующий часовой пояс IANA.") from None


def _civil_day_start(day, zone):
    midnight = datetime.combine(day, time.min, zone)
    first = midnight.astimezone(datetime_timezone.utc)
    if first.astimezone(zone).replace(tzinfo=None) == midnight.replace(tzinfo=None):
        return first

    # A forward transition can skip midnight. The two offsets bracket the
    # first instant whose local date reaches the requested date.
    other = midnight.replace(fold=1).astimezone(datetime_timezone.utc)
    lower, upper = sorted((first, other))
    while upper - lower > timedelta.resolution:
        midpoint = lower + (upper - lower) // 2
        if midpoint.astimezone(zone).date() < day:
            lower = midpoint
        else:
            upper = midpoint
    return upper


def _date_bounds(start, end, timezone_name):
    if not 1 <= (end - start).days <= MAX_RANGE_DAYS:
        raise CalendarValidation("Выберите диапазон от 1 до 42 дней. Конец диапазона не включается.")
    zone = event_zone(timezone_name)
    try:
        lower = _civil_day_start(start, zone)
        upper = _civil_day_start(end, zone)
        return lower, upper, lower - ALL_DAY_RANGE_MARGIN, upper + ALL_DAY_RANGE_MARGIN
    except (OverflowError, ValueError):
        raise CalendarValidation("Выбранные даты выходят за допустимый диапазон.") from None


def _contexts(project, department):
    contexts = []
    if project is not None:
        contexts.append(project_context(project))
    if department is not None:
        contexts.append(None if department.is_archived else department)
    return contexts or [None]


def _allows(access, action, project, department):
    return all(access.allows(action, context) for context in _contexts(project, department))


def _can_read(access, project, department):
    return (
        (project is None or can_view_project(access, project))
        and _allows(access, READ, project, department)
    )


def _can_create(access, project, department):
    scoped = project is not None or department is not None
    return (
        _can_read(access, project, department)
        and _allows(access, CREATE, project, department)
        and (not scoped or _allows(access, MANAGE_SCOPE, project, department))
    )


def can_open_calendar(access):
    from .invitation_services import has_active_invitation

    return (
        access.allows(READ)
        or any(
            access.allows(READ, department)
            for department in access.organization.departments.filter(is_archived=False)
        )
        or can_open_tasks(access)
        or has_active_invitation(access)
    )


def calendar_filters(*, access):
    projects = [
        {"id": project.pk, "name": project.name, "can_create": not project.is_archived and _can_create(access, project, None)}
        for project in visible_projects(access=access)
    ]
    departments = [
        {"id": department.pk, "name": department.name, "can_create": _can_create(access, None, department)}
        for department in access.organization.departments.filter(is_archived=False).order_by("id")
    ]
    can_create_organization = _can_create(access, None, None)
    return {
        "projects": projects,
        "departments": departments,
        "can_create_organization": can_create_organization,
        "can_create": can_create_organization or any(item["can_create"] for item in [*projects, *departments]),
    }


def _event_rows(access, *, lock=False):
    from .invitation_services import annotate_invited

    rows = access.organization.calendar_events.select_related(
        "department", "project__responsible_department", "recurrence_rule",
    ).annotate(_active_project_member=Exists(ProjectMembership.objects.filter(
        organization=access.organization, project_id=OuterRef("project_id"),
        organization_membership=access.membership, is_active=True,
    )))
    rows = annotate_invited(rows, access)
    return rows.select_for_update(of=("self",)) if lock else rows


def _can_read_event(access, event):
    if event.project_id is not None and hasattr(event, "_active_project_member"):
        event.project._active_team_member = event._active_project_member
    return _can_read(access, event.project, event.department)


def visible_event(*, access, event_id, lock=False, allow_invitation=False):
    event = get_object_or_404(_event_rows(access, lock=lock), pk=event_id)
    if not (_can_read_event(access, event) or (allow_invitation and event._invitation_id is not None)):
        raise Http404
    return event


def _all_day_overlaps(event, start, end):
    zone = event_zone(event.timezone)
    return event.starts_at.astimezone(zone).date() < end and event.ends_at.astimezone(zone).date() > start


def _event_scope_rows(access, *, source, project_id, department_id, include_archived):
    rows = _event_rows(access)
    if source is not None:
        rows = rows.filter(event_type=source)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if department_id is not None:
        rows = rows.filter(department_id=department_id)
    if not include_archived:
        rows = rows.exclude(status=CalendarEvent.Status.ARCHIVED)
    return rows


def _range_event_items(
    *, access, start, end, bounds, timezone_name, source, project_id, department_id,
    include_archived, include_cancelled,
):
    from .recurrence import expand_event_occurrences, occurrence_item, prepare_occurrence_range

    lower, upper, extended_lower, extended_upper = bounds
    rows = _event_scope_rows(
        access, source=source, project_id=project_id, department_id=department_id,
        include_archived=include_archived,
    )
    can_include_invited = project_id is None and department_id is None
    ordinary = rows.filter(recurrence_rule__isnull=True).filter(
        Q(all_day=False, starts_at__lt=upper, ends_at__gt=lower)
        | Q(all_day=True, starts_at__lt=extended_upper, ends_at__gt=extended_lower),
    )
    for event in ordinary.iterator(chunk_size=200):
        if (
            _can_read_event(access, event) or (can_include_invited and event._invitation_id is not None)
        ) and (not event.all_day or _all_day_overlaps(event, start, end)):
            yield event_item(access, event)

    series = prepare_occurrence_range(rows, start=start, end=end, bounds=bounds)
    for event in series.iterator(chunk_size=200):
        if not (_can_read_event(access, event) or (can_include_invited and event._invitation_id is not None)):
            continue
        for occurrence in expand_event_occurrences(
            event, start=start, end=end, timezone_name=timezone_name, include_cancelled=include_cancelled,
            exceptions=event._range_exceptions, bounds=bounds,
        ):
            yield occurrence_item(access, event, occurrence)


def event_permissions(access, event):
    if not _can_read_event(access, event):
        return dict.fromkeys(("can_edit", "can_cancel", "can_archive", "can_manage_scope"), False)
    project, department = event.project, event.department
    scheduled = event.status == CalendarEvent.Status.SCHEDULED
    can_edit = scheduled and _allows(access, UPDATE, project, department)
    return {
        "can_edit": can_edit,
        "can_cancel": scheduled and _allows(access, CANCEL, project, department),
        "can_archive": event.status != CalendarEvent.Status.ARCHIVED and _allows(access, ARCHIVE, project, department),
        "can_manage_scope": can_edit and _allows(access, MANAGE_SCOPE, project, department),
    }


def event_item(access, event):
    from .invitation_services import can_invite, can_manage_invitations
    from .recurrence import rule_data

    full_access = _can_read_event(access, event)
    rule = getattr(event, "recurrence_rule", None)
    return {
        "id": event.pk,
        "source": event.event_type,
        "type": event.event_type,
        "title": event.title,
        "description": event.description if full_access else "",
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "timezone": event.timezone,
        "all_day": event.all_day,
        "status": event.status,
        "status_label": event.get_status_display(),
        "project_id": event.project_id if full_access else None,
        "department_id": event.department_id if full_access else None,
        "location": event.location or None,
        "online_url": event.online_url or None,
        "url": None,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "rsvp_mode": event.rsvp_mode,
        "rsvp_revision": event.rsvp_revision,
        "invitation_id": getattr(event, "_invitation_id", None),
        "can_invite": full_access and can_invite(access, event),
        "can_manage_invitations": full_access and can_manage_invitations(access, event),
        "is_recurring": rule is not None,
        "series_id": event.pk if rule is not None else None,
        "recurrence": rule_data(rule) if rule is not None else None,
        **event_permissions(access, event),
    }


def _task_item(access, task, timezone_name):
    status = task_workflow_status(access, task)
    return {
        "id": task.pk,
        "source": "task_deadline",
        "title": task.name,
        "description": task.description,
        "starts_at": task.due_at,
        "ends_at": None,
        "timezone": timezone_name,
        "all_day": False,
        "status": task.status,
        "status_label": status.name if status is not None else task.get_status_display(),
        "project_id": task.project_id,
        "department_id": task.department_id,
        "location": None,
        "online_url": None,
        "url": reverse("organization-task", kwargs={"org_id": task.organization_id, "task_id": task.pk}),
        "can_edit": False,
        "can_cancel": False,
        "can_archive": False,
    }


def _bounded_items(rows):
    items = list(islice(rows, MAX_CALENDAR_ITEMS + 1))
    if len(items) > MAX_CALENDAR_ITEMS:
        raise CalendarValidation("В периоде слишком много записей. Уменьшите диапазон или примените фильтры.")
    return items


def _calendar_order(item):
    return item["starts_at"], item["source"], item["id"], str(item.get("occurrence_key") or "")


def list_events(
    *, access, start, end, timezone_name, source=None, project_id=None, department_id=None,
    include_archived=False, include_cancelled=False,
):
    rows = _range_event_items(
        access=access, start=start, end=end, bounds=_date_bounds(start, end, timezone_name),
        timezone_name=timezone_name, source=source, project_id=project_id, department_id=department_id,
        include_archived=include_archived, include_cancelled=include_cancelled,
    )
    return sorted(_bounded_items(rows), key=_calendar_order)


def _feed_rows(*, access, start, end, timezone_name, source, project_id, department_id, include_archived, include_cancelled):
    bounds = _date_bounds(start, end, timezone_name)
    if source != "task_deadline":
        yield from _range_event_items(
            access=access, start=start, end=end, bounds=bounds, timezone_name=timezone_name, source=source,
            project_id=project_id, department_id=department_id, include_archived=include_archived,
            include_cancelled=include_cancelled,
        )
    if source not in CalendarEvent.Type.values:
        tasks = visible_task_deadlines(
            access=access, start=bounds[0], end=bounds[1], project_id=project_id,
            department_id=department_id, include_archived=include_archived,
        )
        for task in tasks.iterator(chunk_size=200):
            yield _task_item(access, task, timezone_name)


def calendar_feed(
    *, access, start, end, timezone_name, source=None, project_id=None, department_id=None,
    include_archived=False, include_cancelled=False,
):
    rows = _feed_rows(
        access=access, start=start, end=end, timezone_name=timezone_name, source=source,
        project_id=project_id, department_id=department_id, include_archived=include_archived,
        include_cancelled=include_cancelled,
    )
    return sorted(_bounded_items(rows), key=_calendar_order)


def _linked_project(access, project_id):
    if project_id is None:
        return None
    project = get_object_or_404(
        access.organization.projects.select_related("responsible_department"), pk=project_id,
    )
    if not can_view_project(access, project):
        raise Http404
    if project.is_archived:
        raise CalendarConflict("Нельзя привязать архивный проект.")
    return project


def _linked_department(access, department_id):
    if department_id is None:
        return None
    department = get_object_or_404(access.organization.departments, pk=department_id)
    if department.is_archived:
        raise CalendarConflict("Нельзя привязать архивное подразделение.")
    return department


def _validate_interval(starts_at, ends_at, timezone_name, all_day):
    zone = event_zone(timezone_name)
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise CalendarValidation("Укажите часовой пояс для начала и окончания.")
    try:
        starts_utc = starts_at.astimezone(datetime_timezone.utc)
        ends_utc = ends_at.astimezone(datetime_timezone.utc)
        if all_day:
            start_day = starts_at.astimezone(zone).date()
            end_day = ends_at.astimezone(zone).date()
            valid_day_bounds = (
                start_day < end_day
                and starts_utc == _civil_day_start(start_day, zone)
                and ends_utc == _civil_day_start(end_day, zone)
            )
    except (OverflowError, ValueError):
        raise CalendarValidation("Выбранные даты выходят за допустимый диапазон.") from None
    if ends_utc <= starts_utc:
        raise CalendarValidation("Окончание должно быть позже начала.")
    if all_day and not valid_day_bounds:
        raise CalendarValidation("Для события на весь день укажите начало двух дат в часовом поясе события.")


@transaction.atomic
def create_event(
    *, user, org_id, title, description, event_type, starts_at, ends_at, timezone_name,
    all_day=False, location="", online_url="", project_id=None, department_id=None,
    rsvp_mode=CalendarEvent.RSVPMode.INFORMATIONAL, recurrence=None,
):
    from .recurrence import install_rule

    access = organization_access(user=user, org_id=org_id, lock=True)
    project = _linked_project(access, project_id)
    department = _linked_department(access, department_id)
    if not _can_create(access, project, department):
        raise PermissionDenied
    if rsvp_mode != CalendarEvent.RSVPMode.INFORMATIONAL and not _allows(access, MANAGE_INVITATIONS, project, department):
        raise PermissionDenied
    _validate_interval(starts_at, ends_at, timezone_name, all_day)
    event = access.organization.calendar_events.create(
        created_by=access.membership, title=title, description=description, event_type=event_type,
        starts_at=starts_at, ends_at=ends_at, timezone=timezone_name, all_day=all_day,
        location=location, online_url=online_url, project=project, department=department, rsvp_mode=rsvp_mode,
    )
    if recurrence is not None:
        install_rule(event, recurrence)
    return event_item(access, event)


@transaction.atomic
def update_event(*, user, org_id, event_id, changes):
    from .recurrence import sync_recurrence_timezone, validate_series_schedule_change

    access = organization_access(user=user, org_id=org_id, lock=True)
    event = visible_event(access=access, event_id=event_id, lock=True)
    if (not changes or changes.keys() - {"rsvp_mode"}) and not _allows(access, UPDATE, event.project, event.department):
        raise PermissionDenied
    if "rsvp_mode" in changes and not _allows(access, MANAGE_INVITATIONS, event.project, event.department):
        raise PermissionDenied
    if event.status != CalendarEvent.Status.SCHEDULED:
        raise CalendarConflict("Изменить можно только запланированное событие.")
    if "recurrence" in changes:
        raise CalendarValidation("Измените правило повтора отдельным действием для всей серии.")
    validate_series_schedule_change(event, changes)

    new_project_id = changes.get("project_id", event.project_id)
    new_department_id = changes.get("department_id", event.department_id)
    fields = []
    if (new_project_id, new_department_id) != (event.project_id, event.department_id):
        if not _allows(access, MANAGE_SCOPE, event.project, event.department):
            raise PermissionDenied
        project = event.project if new_project_id == event.project_id else _linked_project(access, new_project_id)
        department = event.department if new_department_id == event.department_id else _linked_department(access, new_department_id)
        if not (
            _can_read(access, project, department)
            and _allows(access, UPDATE, project, department)
            and _allows(access, MANAGE_SCOPE, project, department)
        ):
            raise PermissionDenied
        if "rsvp_mode" in changes and not _allows(access, MANAGE_INVITATIONS, project, department):
            raise PermissionDenied
        event.project = project
        event.department = department
        fields.extend(["project", "department"])
    schedule_changed = any(
        field in changes and changes[field] != getattr(event, field)
        for field in ("starts_at", "ends_at", "timezone", "all_day")
    )
    new_mode = changes.get("rsvp_mode", event.rsvp_mode)
    informational_transition = new_mode != event.rsvp_mode and CalendarEvent.RSVPMode.INFORMATIONAL in (
        event.rsvp_mode, new_mode,
    )
    for field in ("title", "description", "event_type", "starts_at", "ends_at", "timezone", "all_day", "location", "online_url", "rsvp_mode"):
        if field in changes:
            setattr(event, field, changes[field])
            fields.append(field)
    _validate_interval(event.starts_at, event.ends_at, event.timezone, event.all_day)
    if schedule_changed or informational_transition:
        event.rsvp_revision += 1
        fields.append("rsvp_revision")
    if fields:
        event.save(update_fields=[*fields, "updated_at"])
        if schedule_changed:
            sync_recurrence_timezone(event)
    return event_item(access, event)


@transaction.atomic
def cancel_event(*, user, org_id, event_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    event = visible_event(access=access, event_id=event_id, lock=True)
    if not _allows(access, CANCEL, event.project, event.department):
        raise PermissionDenied
    if event.status == CalendarEvent.Status.ARCHIVED:
        raise CalendarConflict("Архивное событие нельзя отменить.")
    if event.status == CalendarEvent.Status.SCHEDULED:
        event.status = CalendarEvent.Status.CANCELLED
        event.cancelled_at = timezone.now()
        event.save(update_fields=["status", "cancelled_at", "updated_at"])
    return event_item(access, event)


@transaction.atomic
def archive_event(*, user, org_id, event_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    event = visible_event(access=access, event_id=event_id, lock=True)
    if not _allows(access, ARCHIVE, event.project, event.department):
        raise PermissionDenied
    if event.status != CalendarEvent.Status.ARCHIVED:
        event.status = CalendarEvent.Status.ARCHIVED
        event.archived_at = timezone.now()
        event.save(update_fields=["status", "archived_at", "updated_at"])
    return event_item(access, event)
