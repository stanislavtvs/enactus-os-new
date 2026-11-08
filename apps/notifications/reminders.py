"""Bounded, restartable scans of current task and calendar schedules."""
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import Http404
from django.utils import timezone

from apps.access.policy import accesses_for_memberships
from apps.calendar_app.invitation_models import CalendarInvitation
from apps.calendar_app.invitation_services import effective_response
from apps.calendar_app.models import CalendarEvent, CalendarOccurrenceRSVP
from apps.calendar_app.occurrence_rsvp import _current_response
from apps.calendar_app.recurrence import (
    _local_start, expand_event_occurrences, prepare_occurrence_range, resolve_occurrence,
)
from apps.calendar_app.services import _date_bounds, event_zone
from apps.organizations.models import Organization
from apps.projects.models import ProjectMembership
from apps.projects.services import _can_view as can_view_project
from apps.tasks.models import Task, TaskCollaborator

from .models import NotificationEvent, NotificationScanCursor, OrganizationNotificationPolicy
from .preferences import preferences_for, quiet_until
from .services import publish

TASK_REMINDER = "task.reminder"
TASK_OVERDUE = "task.overdue"
CALENDAR_REMINDER = "calendar.reminder"
GRACE = timedelta(hours=1)
DAY = timedelta(days=1)
OVERDUE_DELAY = timedelta(minutes=1)


def _task_rows():
    return Task.objects.filter(is_archived=False, due_at__isnull=False).exclude(
        Q(status__in=(Task.Status.COMPLETED, Task.Status.CANCELLED)) | Q(project__is_archived=True),
    )


def _task_details(rows):
    collaborators = TaskCollaborator.objects.filter(
        is_active=True, organization_membership__is_active=True, organization_membership__user__is_active=True,
    ).select_related("organization_membership__user")
    return rows.select_related("assignee__user", "project__responsible_department").prefetch_related(
        Prefetch("collaborators", queryset=collaborators, to_attr="_reminder_collaborators"),
    )


def _task_members(task, policy):
    if task.assignee_id and task.assignee.is_active and task.assignee.user.is_active:
        yield task.assignee
    if policy.include_collaborators:
        yield from (row.organization_membership for row in task._reminder_collaborators)


def _task_access(organization, tasks, policy):
    memberships = {member.pk: member for task in tasks for member in _task_members(task, policy)}
    project_ids = {task.project_id for task in tasks if task.project_id}
    teams = set(ProjectMembership.objects.filter(
        organization=organization, project_id__in=project_ids,
        organization_membership_id__in=memberships, is_active=True,
    ).values_list("project_id", "organization_membership_id"))
    return accesses_for_memberships(organization, memberships.values()), teams


def _can_read_task(task, member, access, teams):
    if task.project_id is None:
        return True
    task.project._active_team_member = (task.project_id, member.pk) in teams
    return can_view_project(access[member.pk], task.project)


def _allowed(policy, preference, kind):
    category = "calendar" if kind == CALENDAR_REMINDER else "tasks"
    return category in policy.enabled_categories and kind not in policy.disabled_kinds and (
        preference is None or (
            category in preference.enabled_categories
            and (preference.calendar_reminders if category == "calendar" else preference.task_reminders)
        )
    )


def _expiry(kind, offset, reference):
    return reference + GRACE if kind == TASK_OVERDUE or (kind == TASK_REMINDER and offset == 0) else reference


def _deliver_after(kind, offset, reference, preference, now):
    deadline_instant = kind == TASK_REMINDER and offset == 0
    if deadline_instant and quiet_until(preference, reference) is not None:
        return False, None
    resume = quiet_until(preference, now)
    if resume is None:
        return True, None
    # Deadline-at reminders cannot be meaningfully postponed past their deadline.
    if deadline_instant or resume >= _expiry(kind, offset, reference):
        return False, None
    return False, resume


def _plan(plans, record, kind, recipient_id, offset, planned, reference, now, preference, *, occurrence=None):
    if not planned <= now <= planned + GRACE or now >= _expiry(kind, offset, reference):
        return
    deliver, resume = _deliver_after(kind, offset, reference, preference, now)
    if not deliver and resume is None:
        return
    reference = reference.astimezone(UTC)
    key_date = occurrence.key if occurrence else "-"
    version = occurrence.rsvp_revision if occurrence else 0
    schedule_version = record.rsvp_revision if kind == CALENDAR_REMINDER else 0
    key = f"{kind}:{record.pk}:{key_date}:{reference.isoformat()}:{schedule_version}:{version}:{offset}:{recipient_id}"
    data = {"planned_at": planned, "reference_at": reference, "offset_minutes": offset,
            "schedule_revision": schedule_version}
    if occurrence:
        data.update(occurrence_key=occurrence.key, occurrence_revision=version)
    plans.append({
        "organization_id": record.organization_id, "actor_id": None, "kind": kind,
        "object_id": record.pk, "recipient_ids": [recipient_id],
        "object_type": "calendar_event" if kind == CALENDAR_REMINDER else "task",
        "key": key, "data": data, "now": now, "deliver_after": resume,
    })


def _publish_plans(organization, plans, budget):
    existing = set()
    keys = [plan["key"] for plan in plans]
    for offset in range(0, len(keys), 1000):
        existing.update(NotificationEvent.objects.filter(
            organization=organization, idempotency_key__in=keys[offset:offset + 1000],
        ).values_list("idempotency_key", flat=True))
    used = 0
    deferred = None
    for plan in plans:
        if plan["key"] in existing:
            continue
        if used == budget:
            object_id = plan["object_id"]
            deferred = object_id if deferred is None else min(deferred, object_id)
            continue
        publish(**plan)
        existing.add(plan["key"])
        used += 1
    return used, deferred


def _scan_tasks(organization, policy, ids, now):
    tasks = list(_task_details(_task_rows().filter(organization=organization, pk__in=ids)))
    access, teams = _task_access(organization, tasks, policy)
    preferences = preferences_for(organization.pk, [actor.user.pk for actor in access.values()])
    plans = []
    for task in tasks:
        for member in _task_members(task, policy):
            if not _can_read_task(task, member, access, teams):
                continue
            preference = preferences.get(member.user_id)
            if _allowed(policy, preference, TASK_REMINDER):
                for offset in policy.task_offsets_minutes:
                    _plan(plans, task, TASK_REMINDER, member.user_id, offset,
                             task.due_at - timedelta(minutes=offset), task.due_at, now, preference)
            if policy.task_overdue and _allowed(policy, preference, TASK_OVERDUE):
                _plan(plans, task, TASK_OVERDUE, member.user_id, 0, task.due_at + OVERDUE_DELAY,
                         task.due_at, now, preference)

    return plans


def _calendar_rows():
    return CalendarEvent.objects.filter(status=CalendarEvent.Status.SCHEDULED)


def _calendar_invitations(rows):
    recipients = CalendarInvitation.objects.filter(
        revoked_at__isnull=True, recipient__is_active=True, recipient__user__is_active=True,
    ).select_related("recipient")
    return rows.prefetch_related(Prefetch("invitations", queryset=recipients, to_attr="_reminder_invitees"))


def _calendar_window(now):
    today = now.astimezone(UTC).date()
    start, end = today - DAY, today + 3 * DAY
    return start, end, _date_bounds(start, end, "UTC")


def _occurrences(rows, now):
    start, end, bounds = _calendar_window(now)
    return prepare_occurrence_range(rows.select_related("recurrence_rule"), start=start, end=end, bounds=bounds)


def _calendar_planned(event, starts_at, offset, policy):
    if not event.all_day:
        return starts_at - timedelta(minutes=offset)
    if offset != 1440:
        return None
    zone = event_zone(event.timezone)
    day = starts_at.astimezone(zone).date() - DAY
    return _local_start(day, policy.all_day_reminder_time, zone)


def _attending(invitation, event, occurrence, reply):
    if occurrence is None:
        response = effective_response(invitation, event)
    elif event.rsvp_mode == CalendarEvent.RSVPMode.INFORMATIONAL:
        response = None
    elif _current_response(reply, event, occurrence):
        response = reply.response
    elif reply is None and occurrence.rsvp_revision == 0:
        response = effective_response(invitation, event)
    else:
        response = None
    return response != CalendarInvitation.Response.NO


def _scan_calendar(organization, policy, ids, now, *, series):
    rows = _calendar_rows().filter(organization=organization, pk__in=ids)
    rows = _occurrences(rows, now) if series else rows.filter(recurrence_rule__isnull=True)
    events = list(_calendar_invitations(rows))
    preferences = preferences_for(organization.pk, {
        row.recipient.user_id for event in events for row in event._reminder_invitees
    })
    start, end, bounds = _calendar_window(now)
    candidates = []
    for event in events:
        if not policy.calendar_informational and event.rsvp_mode == CalendarEvent.RSVPMode.INFORMATIONAL:
            continue
        occurrences = expand_event_occurrences(
            event, start=start, end=end, timezone_name="UTC", exceptions=event._range_exceptions, bounds=bounds,
        ) if series else [None]
        for occurrence in occurrences:
            starts_at = occurrence.starts_at if occurrence else event.starts_at
            if now < starts_at <= now + 2 * DAY:
                candidates.append((event, occurrence, starts_at))

    keys = {occurrence.key for _, occurrence, _ in candidates if occurrence}
    replies = {
        (row.invitation_id, row.occurrence_key): row for row in CalendarOccurrenceRSVP.objects.filter(
            event_id__in=ids, organization=organization, occurrence_key__in=keys,
        )
    }
    plans = []
    for event, occurrence, starts_at in candidates:
        for invitation in event._reminder_invitees:
            recipient_id = invitation.recipient.user_id
            preference = preferences.get(recipient_id)
            if not _allowed(policy, preference, CALENDAR_REMINDER):
                continue
            reply = replies.get((invitation.pk, occurrence.key)) if occurrence else None
            if not _attending(invitation, event, occurrence, reply):
                continue
            for offset in policy.calendar_offsets_minutes:
                planned = _calendar_planned(event, starts_at, offset, policy)
                if planned is not None:
                    _plan(plans, event, CALENDAR_REMINDER, recipient_id, offset, planned, starts_at, now,
                             preference, occurrence=occurrence)

    return plans


@transaction.atomic
def _scan_batch(stream, rows, limit, now):
    NotificationScanCursor.objects.get_or_create(stream=stream)
    cursor = NotificationScanCursor.objects.select_for_update(skip_locked=True).filter(pk=stream).first()
    if cursor is None:
        return 0
    batch = list(rows.filter(pk__gt=cursor.last_id).order_by("pk").values_list("pk", "organization_id")[:limit])
    groups = defaultdict(list)
    for object_id, org_id in batch:
        groups[org_id].append(object_id)
    budget = limit
    resume_from = None
    for org_id, ids in sorted(groups.items()):
        organization = Organization.objects.select_for_update(skip_locked=True).filter(pk=org_id).first()
        if organization is None:
            continue
        policy = OrganizationNotificationPolicy.objects.filter(organization=organization).first()
        if policy is None:
            continue
        if stream == "task":
            plans = _scan_tasks(organization, policy, ids, now)
        elif policy.calendar_offsets_minutes:
            plans = _scan_calendar(organization, policy, ids, now, series=stream == "calendar_series")
        else:
            continue
        used, deferred = _publish_plans(organization, plans, budget)
        budget -= used
        if deferred is not None:
            resume_from = deferred if resume_from is None else min(resume_from, deferred)
    cursor.last_id = resume_from - 1 if resume_from is not None else (batch[-1][0] if len(batch) == limit else 0)
    cursor.save(update_fields=["last_id"])
    return len(batch)


def run_reminders(*, now=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Reminder batch limit must be between 1 and 1000.")
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise ValueError("Reminder clock must include a timezone.")
    policies = OrganizationNotificationPolicy.objects.all()
    task_orgs = policies.filter(Q(task_overdue=True) | ~Q(task_offsets_minutes=[])).values("organization_id")
    calendar_orgs = policies.exclude(calendar_offsets_minutes=[]).values("organization_id")
    tasks = _task_rows().filter(organization_id__in=task_orgs, due_at__gte=now - GRACE, due_at__lte=now + DAY)
    events = _calendar_rows().filter(organization_id__in=calendar_orgs)
    ordinary = events.filter(recurrence_rule__isnull=True, starts_at__gt=now, starts_at__lte=now + 2 * DAY)
    return {
        "tasks": _scan_batch("task", tasks, limit, now),
        "calendar": _scan_batch("calendar", ordinary, limit, now),
        "series": _scan_batch("calendar_series", _occurrences(events, now), limit, now),
    }


def reminder_delivery(event, now):
    """Recheck pending reminders inside the delivery transaction before making them visible."""
    organization = Organization.objects.select_for_update(skip_locked=True).filter(pk=event.organization_id).first()
    if organization is None:
        return False, now + timedelta(seconds=30)
    policy = OrganizationNotificationPolicy.objects.filter(organization=organization).first()
    if policy is None or len(event.recipient_ids) != 1:
        return False, None
    recipient_id = event.recipient_ids[0]
    preference = preferences_for(organization.pk, [recipient_id]).get(recipient_id)
    if not _allowed(policy, preference, event.kind):
        return False, None
    try:
        reference = datetime.fromisoformat(event.data["reference_at"])
        offset = event.data["offset_minutes"]
    except (KeyError, TypeError, ValueError):
        return False, None
    if now >= _expiry(event.kind, offset, reference):
        return False, None

    if event.kind in (TASK_REMINDER, TASK_OVERDUE):
        task = _task_details(_task_rows().filter(organization=organization, pk=event.object_id)).first()
        enabled = policy.task_overdue if event.kind == TASK_OVERDUE else offset in policy.task_offsets_minutes
        if not enabled or task is None or task.due_at != reference:
            return False, None
        access, teams = _task_access(organization, [task], policy)
        if not any(member.user_id == recipient_id and _can_read_task(task, member, access, teams)
                   for member in _task_members(task, policy)):
            return False, None
    elif event.kind == CALENDAR_REMINDER:
        calendar_event = _calendar_rows().filter(organization=organization, pk=event.object_id).select_related(
            "recurrence_rule",
        ).first()
        if (
            calendar_event is None or offset not in policy.calendar_offsets_minutes
            or calendar_event.rsvp_revision != event.data.get("schedule_revision", 0)
            or (calendar_event.rsvp_mode == CalendarEvent.RSVPMode.INFORMATIONAL and not policy.calendar_informational)
        ):
            return False, None
        invitation = calendar_event.invitations.filter(
            recipient__user_id=recipient_id, recipient__is_active=True, recipient__user__is_active=True,
            revoked_at__isnull=True,
        ).first()
        if invitation is None:
            return False, None
        key = event.data.get("occurrence_key")
        occurrence = reply = None
        if key:
            try:
                occurrence = resolve_occurrence(calendar_event, datetime.fromisoformat(key).date())
            except (Http404, ValueError):
                return False, None
            if (
                occurrence.status != CalendarEvent.Status.SCHEDULED or occurrence.starts_at != reference
                or occurrence.rsvp_revision != event.data.get("occurrence_revision", 0)
            ):
                return False, None
            reply = invitation.occurrence_responses.filter(occurrence_key=occurrence.key).first()
        elif calendar_event.starts_at != reference or getattr(calendar_event, "recurrence_rule", None) is not None:
            return False, None
        if not _attending(invitation, calendar_event, occurrence, reply):
            return False, None
    return _deliver_after(event.kind, offset, reference, preference, now)
