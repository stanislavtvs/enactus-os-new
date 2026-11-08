from apps.audit.producer import affected_ids, record_change, snapshot
from apps.audit.services import record

EVENT_FIELDS = (
    "title", "description", "event_type", "starts_at", "ends_at", "timezone",
    "all_day", "location", "online_url", "project_id", "department_id", "rsvp_mode", "status",
)


def _scope(event):
    departments = []
    if event.department_id:
        departments.append(event.department_id)
    if event.project_id:
        departments.append(event.project.responsible_department_id)
    return departments or [None]


def _scope_metadata(event, previous=()):
    departments = [*_scope(event), *previous]
    ids = affected_ids(*departments)
    if not ids or len(ids) == 1 and None not in departments:
        return {}
    return {"affected_department_ids": ids, "organization_scope": None in departments}


def event_snapshot(event):
    return {**snapshot(event, EVENT_FIELDS), "audit_department_ids": _scope(event)}


def event_change(user, event, verb, before=None, metadata=None):
    scope = _scope_metadata(event)
    if before and (
        before["project_id"] != event.project_id or before["department_id"] != event.department_id
    ):
        scope = _scope_metadata(event, before["audit_department_ids"])
        scope["affected_project_ids"] = affected_ids(before["project_id"], event.project_id)
    record_change(
        user=user, action=f"calendar_event.{verb}", instance=event, before=before, fields=EVENT_FIELDS,
        department_id=next((pk for pk in _scope(event) if pk is not None), None), project_id=event.project_id,
        metadata=scope | (metadata or {}),
    )


def calendar_action(user, event, verb, *, metadata=None, before=None, after=None):
    before, after = before or {}, after or {}
    record(
        organization_id=event.organization_id, actor_id=user.pk, action=f"calendar_event.{verb}",
        object_type="calendar_event", object_id=event.pk,
        department_id=next((pk for pk in _scope(event) if pk is not None), None), project_id=event.project_id,
        changes={field: {"old": before.get(field), "new": after.get(field)} for field in before.keys() | after.keys()},
        metadata=_scope_metadata(event) | (metadata or {}),
    )
