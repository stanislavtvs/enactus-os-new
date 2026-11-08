from datetime import datetime, time, timedelta, timezone
from itertools import islice

from django.db.models import F, Q
from django.utils.timezone import now

from apps.calendar_app.models import CalendarEvent
from apps.calendar_app.services import CalendarValidation, can_open_calendar, event_zone, list_events
from apps.projects.models import Project
from apps.organizations.modules import enabled
from apps.tasks.models import Task
from apps.tasks.reviews import can_open_review_inbox, visible_review_stages
from apps.tasks.services import TERMINAL_STATUSES, _task_rows, _visible_rows


PREVIEW_LIMIT = 5
DASHBOARD_DAYS = 7


def dashboard(access, projects):
    current = now()
    first_day = current.date()
    last_day = first_day + timedelta(days=DASHBOARD_DAYS)
    deadline_limit = datetime.combine(last_day, time.min, tzinfo=timezone.utc)
    rows = _task_rows(access).prefetch_related(None).filter(is_archived=False).exclude(
        Q(status__in=TERMINAL_STATUSES) | Q(project__is_archived=True),
    )
    visible = _visible_rows(access, rows)
    mine = visible.filter(Q(assignee=access.membership) | Q(_my_collaboration=True))
    deadline_order = (F("due_at").asc(nulls_last=True), "pk")
    attention = mine.filter(
        Q(due_at__lt=current) | Q(status=Task.Status.PENDING, _strict_dependencies_unresolved=True),
    )
    calendar_available = can_open_calendar(access)
    calendar_overflow = False
    events = []
    if calendar_available:
        try:
            events = list_events(access=access, start=first_day, end=last_day, timezone_name="UTC")
        except CalendarValidation:
            calendar_overflow = True
    upcoming_events = [
        {**event, "dashboard_day": event["starts_at"].astimezone(event_zone(event["timezone"])).date()}
        for event in events
        if event["status"] == CalendarEvent.Status.SCHEDULED
        and (event["all_day"] or event["ends_at"] > current)
    ][:PREVIEW_LIMIT]
    return {
        "tasks_available": enabled(access.organization, "tasks"),
        "review_tasks": [
            stage.task for stage in islice(visible_review_stages(access), PREVIEW_LIMIT)
        ] if can_open_review_inbox(access) else [],
        "personal_tasks": list(mine.order_by(*deadline_order)[:PREVIEW_LIMIT]),
        "attention_tasks": list(attention.order_by(*deadline_order)[:PREVIEW_LIMIT]),
        "upcoming_deadlines": list(visible.filter(
            due_at__gte=current, due_at__lt=deadline_limit,
        ).order_by(*deadline_order)[:PREVIEW_LIMIT]),
        "active_projects": [
            project for project in reversed(projects) if not project.is_archived and project.status == Project.Status.ACTIVE
        ][:PREVIEW_LIMIT],
        "calendar_available": calendar_available,
        "calendar_overflow": calendar_overflow,
        "upcoming_events": upcoming_events,
        "dashboard_now": current,
    }
