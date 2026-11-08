from django.db.models import Exists, OuterRef, Q

PROJECT_STAFFING_KINDS = ("project.allocated", "project.invited")
OBJECT_MODULES = {
    "projects": ("project",),
    "staffing": ("staffing_request", "project_invitation"),
    "tasks": ("task",),
    "calendar": ("calendar_event",),
}


def exclude_disabled_events(rows, event_prefix=""):
    from apps.calendar_app.models import CalendarEvent
    from apps.tasks.models import Task

    field = f"{event_prefix}object_type"
    org = f"{event_prefix}organization_id"
    target = f"{event_prefix}object_id"
    disabled = f"{event_prefix}organization__disabled_modules__contains"
    blocked = Q()
    for module, types in OBJECT_MODULES.items():
        blocked |= Q(**{f"{field}__in": types, disabled: [module]})
    blocked |= Q(**{f"{event_prefix}kind__in": PROJECT_STAFFING_KINDS, disabled: ["staffing"]})
    for model, object_type in ((Task, "task"), (CalendarEvent, "calendar_event")):
        linked = model.objects.filter(
            pk=OuterRef(target), organization_id=OuterRef(org), project_id__isnull=False,
        )
        blocked |= Q(**{field: object_type, disabled: ["projects"]}) & Q(Exists(linked))
    return rows.exclude(blocked)
