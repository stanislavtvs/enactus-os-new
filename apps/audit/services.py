import re
from datetime import date, datetime, time, timezone
from itertools import islice
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.access.policy import Access
from apps.organizations.models import Organization

from .context import current_operation_id
from .models import AuditEvent

OBJECT_LABELS = {
    "organization": "Организация",
    "ownership_transfer": "Предложение передачи владения",
    "organization_invitation": "Приглашение в организацию",
    "join_request": "Заявка на вступление",
    "department": "Подразделение",
    "department_invitation": "Приглашение в подразделение",
    "department_membership": "Членство в подразделении",
    "role": "Роль",
    "role_assignment": "Назначение роли",
    "position_assignment": "Должность",
    "project": "Проект",
    "staffing_request": "Запрос специалистов",
    "staffing_allocation": "Распределение специалиста",
    "task": "Задача",
    "calendar_event": "Событие календаря",
    "notification_policy": "Политика уведомлений",
}
OWNERSHIP_TRANSFERRED = "organization.ownership_transferred"

ACTION_LABELS = {
    "ownership_transferred": "Передача владения", "member_added": "Включение участника",
    "created": "Создание", "updated": "Изменение", "archived": "Архивирование",
    "moved": "Перемещение", "assigned": "Назначение", "revoked": "Отзыв",
    "invited": "Приглашение", "accepted": "Принятие", "declined": "Отклонение",
    "approved": "Утверждение", "rejected": "Отклонение", "cancelled": "Отмена",
    "submitted": "Отправка", "returned": "Возврат", "removed": "Исключение",
    "offered": "Предложение передачи", "expired": "Истечение срока",
    "issued": "Приглашение", "join_policy_changed": "Изменение правила вступления",
    "member_management_changed": "Изменение полномочий участника",
    "status_changed": "Изменение статуса", "parent_changed": "Перенос подзадачи",
    "unassigned": "Снятие исполнителя", "collaborator_added": "Назначение соисполнителя",
    "collaborator_removed": "Снятие соисполнителя", "routed": "Направление на распределение",
    "review_submitted": "Отправка на проверку", "review_approved": "Принятие результата",
    "review_returned": "Возврат на доработку", "review_policy_changed": "Изменение маршрута проверки",
    "checklist_converted": "Преобразование задачи и пункта чек-листа",
    "dependency_added": "Добавление зависимости", "dependency_removed": "Удаление зависимости",
    "lead_changed": "Смена руководителя", "department_changed": "Смена подразделения",
    "member_removed": "Исключение участника", "project_assignment_mode_changed": "Изменение порядка назначения",
    "recurrence_changed": "Изменение повторений", "recurrence_removed": "Удаление повторений",
    "series_stopped": "Остановка серии", "occurrence_updated": "Изменение повторения",
    "occurrence_cancelled": "Отмена повторения", "invitations_sent": "Отправка приглашений",
    "invitation_revoked": "Отзыв приглашения", "rsvp_recorded": "Ответ на приглашение",
    "rsvp_occurrence_recorded": "Ответ на повторение",
    "invitation_audience_changed": "Изменение аудитории приглашений",
    "department_added": "Подключение подразделения", "department_removed": "Исключение подразделения",
    "fulfilled": "Выполнение заявки", "partial": "Частичное выполнение заявки",
}
SCALAR_FIELDS = frozenset("""
    name title status stage priority priority_id priority_level_id workflow_status_id
    parent_id department_id project_id responsible_department_id source_department_id
    lead_membership_id organization_membership_id membership_id owner_id recipient_id
    assignee_id reviewer_id role_id department_membership_id predecessor_id
    start_date planned_end_date starts_at ends_at planned_start_at due_at completed_at
    timezone all_day is_archived is_active can_manage_members include_descendants
    join_policy department_directory_visibility project_assignment_mode task_assignment_mode
    review_policy task_review_policy rsvp_mode rsvp_revision occurrence_key response type event_type kind mode color
    code rank sort_order is_default is_enabled allow_assignee requested_count duration_days
    desired_start task_overdue include_collaborators calendar_informational all_day_reminder_time
    frequency interval until count stopped_from is_cancelled occurrence_revision
""".split())
LIST_FIELDS = frozenset("""
    permissions required_permissions enabled_categories disabled_kinds
    task_offsets_minutes calendar_offsets_minutes weekdays department_ids project_ids
    member_ids collaborator_ids participant_ids affected_department_ids affected_project_ids
""".split())
REDACTED_FIELDS = frozenset("""
    description location online_url comment note reason specialization review_comment
""".split())
METADATA_FIELDS = SCALAR_FIELDS - {"name", "title"} | LIST_FIELDS | frozenset("""
    affected_count count added_count removed_count updated_count invitation_count
    request_id submission_id stage_id allocation_id dependency_id checklist_id item_id
    invitation_id occurrence_version before_count after_count organization_scope history_id
    distribution_id submission_version stage_position
""".split())
STABLE_CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
MAX_TEXT = 250
BATCH_SIZE = 200
PARTICIPANT_HISTORY_TYPES = frozenset({"task", "project", "calendar_event", "department"})


def _safe_value(value, *, nested=False):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, str) and "\x00" not in value and len(value) <= MAX_TEXT:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return None
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)) and not nested:
        return [_safe_value(item, nested=True) for item in value[:100]]
    if isinstance(value, dict) and set(value) <= {"code", "include_descendants"}:
        return {
            key: _safe_value(item, nested=True)
            for key, item in value.items()
            if isinstance(item, (str, bool))
        }
    return None


def _safe_fields(fields, allowed):
    result = {key: _safe_value(value) for key, value in (fields or {}).items() if key in allowed}
    for key in ("affected_department_ids", "affected_project_ids"):
        if key not in result:
            continue
        scope_ids = fields[key]
        if not isinstance(scope_ids, (list, tuple)) or any(
            type(pk) is not int or not 0 < pk < 2**63 for pk in scope_ids
        ):
            raise ValueError("Audit scopes must contain valid IDs.")
        result[key] = sorted(set(scope_ids))
    if "organization_scope" in result and type(result["organization_scope"]) is not bool:
        raise ValueError("Audit organization scope must be a boolean.")
    return result


def _safe_changes(changes):
    result = {}
    for field, values in (changes or {}).items():
        if not isinstance(values, dict) or set(values) != {"old", "new"}:
            continue
        if values["old"] == values["new"]:
            continue
        if field in REDACTED_FIELDS:
            result[field] = {"old": None, "new": None}
        elif field in SCALAR_FIELDS | LIST_FIELDS:
            result[field] = _safe_fields(values, {"old", "new"})
    return result


def record(
    *, organization_id, actor_id, action, object_type, object_id,
    department_id=None, project_id=None, changes=None, metadata=None,
    idempotency_key=None, operation_id=None,
):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Audit events must be recorded in the business transaction.")
    if (
        object_type not in OBJECT_LABELS
        or not isinstance(action, str)
        or len(action) > 80
        or not STABLE_CODE.fullmatch(action)
    ):
        raise ValueError("Unknown audit object type or invalid action code.")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or len(idempotency_key) > 255
        or "\x00" in idempotency_key
    ):
        raise ValueError("Invalid audit idempotency key.")
    values = {
        "actor_id": actor_id, "action": action, "object_type": object_type,
        "object_id": object_id, "department_id": department_id, "project_id": project_id,
        "changes": _safe_changes(changes), "metadata": _safe_fields(metadata, METADATA_FIELDS),
    }
    operation_id = operation_id if operation_id is not None else current_operation_id.get()
    if operation_id is not None:
        values["operation_id"] = UUID(str(operation_id))
    if idempotency_key is None:
        return AuditEvent.objects.create(organization_id=organization_id, **values)
    event, created = AuditEvent.objects.get_or_create(
        organization_id=organization_id, idempotency_key=idempotency_key, defaults=values,
    )
    if not created and (event.action, event.object_type, event.object_id) != (action, object_type, object_id):
        raise ValueError("Audit idempotency key already belongs to another event.")
    return event


def can_open_audit(access):
    return (
        access.allows("audit.read_all")
        or access.allows("audit.read_scoped")
        or bool(access.membership and access._grants.get("audit.read_scoped"))
    )


def _access(user, org_id, *, require_audit=True):
    organization = get_object_or_404(Organization, pk=org_id)
    access = Access(user, organization)
    if access.membership is None:
        raise Http404
    if require_audit and not can_open_audit(access):
        raise PermissionDenied
    return access


def _department_ids(access, permission):
    departments = access.organization.departments.filter(is_archived=False)
    return {department.pk for department in departments if access.allows(permission, department)}


def _scopes(access):
    read = None if access.allows("audit.read_all") else set()
    if read is not None and access._grants.get("audit.read_scoped"):
        read = _department_ids(access, "audit.read_scoped")
    details = None if access.allows("audit.read_details") else set()
    if details is not None and access._grants.get("audit.read_details"):
        details = _department_ids(access, "audit.read_details")
    return read, details


def _in_scope(event, scoped_ids):
    return scoped_ids is None or (
        event.department_id in scoped_ids
        and not event.metadata.get("organization_scope")
        and scoped_ids.issuperset(event.metadata.get("affected_department_ids") or [])
    )


def _rows(access, scoped_ids):
    rows = AuditEvent.objects.filter(organization=access.organization).select_related("actor")
    if scoped_ids is not None:
        rows = rows.filter(department_id__in=scoped_ids)
    return rows.order_by("-created_at", "-pk")


def _action_label(action):
    return ACTION_LABELS.get(action.rsplit(".", 1)[-1], "Изменение")


def _visible_batches(access, rows, scoped_ids):
    from .visibility import present_events

    iterator = rows.iterator(chunk_size=BATCH_SIZE)
    while batch := list(islice(iterator, BATCH_SIZE)):
        if scoped_ids is not None:
            batch = [event for event in batch if _in_scope(event, scoped_ids)]
        if not batch:
            continue
        presentations = present_events(access, batch)
        for event in batch:
            if event.pk in presentations:
                yield event, presentations[event.pk]


def _summary(event, presentation, detail_ids):
    object_title = presentation.get("object_title")
    object_url = presentation.get("object_url")
    object_visible = presentation.get("can_view_object", bool(object_title or object_url))
    department_name = presentation.get("department_name")
    project_name = presentation.get("project_name")
    actor_name = event.actor.get_username() if event.actor_id else None
    can_view_details = object_visible and _in_scope(event, detail_ids)
    return {
        "id": event.pk, "action": event.action, "action_label": _action_label(event.action),
        "object_type": event.object_type,
        "object_type_label": OBJECT_LABELS.get(event.object_type, event.object_type),
        "object_id": event.object_id if object_visible else None,
        "object_title": object_title, "object_url": object_url,
        "actor_id": event.actor_id, "actor_name": actor_name,
        "department_id": event.department_id if department_name else None,
        "department_name": department_name,
        "project_id": event.project_id if project_name else None, "project_name": project_name,
        "created_at": event.created_at, "can_view_details": can_view_details,
    }


def list_events(*, user, org_id, filters, object_ref=None):
    participant_history = object_ref is not None and object_ref[0] in PARTICIPANT_HISTORY_TYPES
    access = _access(user, org_id, require_audit=not participant_history)
    scoped_ids, detail_ids = _scopes(access)
    batch_scope = None if participant_history else scoped_ids
    rows = _rows(access, batch_scope)
    if object_ref is not None:
        object_type, object_id = object_ref
        if object_type not in OBJECT_LABELS:
            raise Http404
        rows = rows.filter(object_type=object_type, object_id=object_id)
        if participant_history:
            from .visibility import can_open_object_history

            if not can_open_object_history(access, object_type, object_id):
                raise Http404
        elif not any(
            _summary(event, presentation, detail_ids)["object_id"] is not None
            for event, presentation in _visible_batches(access, rows, batch_scope)
        ):
            raise Http404
    for field in ("actor_id", "action", "object_type", "project_id", "department_id"):
        if (value := getattr(filters, field)) is not None:
            rows = rows.filter(**{field: value})
    if filters.date_from is not None:
        rows = rows.filter(created_at__gte=datetime.combine(filters.date_from, time.min, timezone.utc))
    if filters.date_to is not None:
        rows = rows.filter(created_at__lte=datetime.combine(filters.date_to, time.max, timezone.utc))

    items = []
    total = 0
    offset = (filters.page - 1) * filters.page_size
    search = filters.q.strip().casefold()
    for event, presentation in _visible_batches(access, rows, batch_scope):
        item = _summary(event, presentation, detail_ids)
        if object_ref is not None and item["object_id"] is None:
            continue
        if filters.project_id is not None and item["project_id"] is None:
            continue
        if filters.department_id is not None and item["department_id"] is None:
            continue
        if participant_history and not _in_scope(event, scoped_ids):
            if filters.actor_id is not None:
                continue
            item.update(actor_id=None, actor_name=None, can_view_details=False)
        if search and search not in (item["object_title"] or "").casefold():
            continue
        if offset <= total < offset + filters.page_size:
            items.append(item)
        total += 1
    return {"items": items, "total": total, "page": filters.page, "page_size": filters.page_size}


def event_detail(*, user, org_id, event_id):
    access = _access(user, org_id)
    scoped_ids, detail_ids = _scopes(access)
    match = next(_visible_batches(access, _rows(access, scoped_ids).filter(pk=event_id), scoped_ids), None)
    if match is None:
        raise Http404
    event, presentation = match
    item = _summary(event, presentation, detail_ids)
    allowed = item["can_view_details"]
    return {
        **item, "changes": presentation.get("detail_changes", event.changes) if allowed else None,
        "metadata": presentation.get("detail_metadata", event.metadata) if allowed else None,
        "operation_id": event.operation_id if allowed else None,
    }


def audit_options(*, user, org_id):
    access = _access(user, org_id)
    scoped_ids, detail_ids = _scopes(access)
    entities = {"actors": {}, "departments": {}, "projects": {}}
    codes = {"actions": {}, "object_types": {}}
    for event, presentation in _visible_batches(access, _rows(access, scoped_ids), scoped_ids):
        item = _summary(event, presentation, detail_ids)
        for source, target in (("actor", "actors"), ("department", "departments"), ("project", "projects")):
            if item[f"{source}_id"] is not None:
                entities[target][item[f"{source}_id"]] = item[f"{source}_name"]
        codes["actions"][item["action"]] = item["action_label"]
        codes["object_types"][item["object_type"]] = item["object_type_label"]
    return {
        **{key: [{"id": pk, "name": name} for pk, name in sorted(values.items())]
           for key, values in entities.items()},
        **{key: [{"code": code, "label": label} for code, label in sorted(values.items())]
           for key, values in codes.items()},
    }
