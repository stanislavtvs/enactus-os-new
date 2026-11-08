from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max, Prefetch
from django.db.models.expressions import RawSQL
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access

from .models import Checklist, ChecklistConversion, ChecklistItem, Task
from .services import (
    ARCHIVE, UPDATE, TaskConflict, TaskValidation,
    _audit_task, _can_create, _context, _managed_task, _parent_for_change, _related_audit_scope, _task_is_writable,
    _task_rows, _task_visible, _visible_rows, create_task, visible_task,
)


def list_subtasks(*, access, task_id, page, page_size):
    parent = visible_task(access=access, task_id=task_id)
    descendants = RawSQL(
        """
        WITH RECURSIVE tree(id) AS (
            SELECT id FROM tasks_task WHERE organization_id = %s AND parent_id = %s
            UNION
            SELECT child.id FROM tasks_task AS child
            JOIN tree ON child.parent_id = tree.id
            WHERE child.organization_id = %s
        )
        SELECT id FROM tree
        """,
        [access.organization.pk, parent.pk, access.organization.pk],
    )
    rows = _visible_rows(access).filter(pk__in=descendants)
    offset = (page - 1) * page_size
    return (
        rows.order_by("id")[offset:offset + page_size],
        rows.count(),
        rows.filter(status=Task.Status.COMPLETED).count(),
    )


def _can_update(access, task, *, require_writable=True):
    return (
        _task_visible(access, task)
        and (not require_writable or _task_is_writable(task))
        and access.allows(UPDATE, _context(task.project, task.department))
    )


def _visible_parent(access, task):
    if task.parent_id is None:
        return None
    parent = _task_rows(access).filter(pk=task.parent_id).first()
    return parent if parent is not None and _task_visible(access, parent) else None


def _reverse_conflict(task, parent):
    if (
        task.status != Task.Status.PENDING or task.assignee_id is not None
        or (task.workflow_status_id is not None and task.workflow_status.code != Task.Status.PENDING)
        or task.description or task.priority != Task.Priority.NORMAL
        or (task.priority_level_id is not None and task.priority_level.code != Task.Priority.NORMAL)
        or task.planned_start_at is not None or task.due_at is not None
        or task.review_policy is not None or task.reviewer_id is not None
        or (task.project_id, task.department_id) != (parent.project_id, parent.department_id)
    ):
        return "У подзадачи есть самостоятельные настройки, сроки, статус или исполнитель."
    if (
        task.subtasks.exists() or task.checklists.exists() or task.collaborators.exists()
        or task.distributions.exists() or task.review_submissions.exists()
        or task.dependencies.exists() or task.dependents.exists()
        or task.checklist_conversions.filter(direction=ChecklistConversion.Direction.TASK_TO_ITEM).exists()
        or task.child_conversions.exists()
    ):
        return "У подзадачи есть дочерние записи или значимая история."
    return None


def get_decomposition(*, access, task_id):
    task = visible_task(access=access, task_id=task_id)
    can_manage = _can_update(access, task)
    parent = _visible_parent(access, task)
    can_move = can_manage and (
        task.parent_id is None or (parent is not None and _can_update(access, parent, require_writable=False))
    )
    can_convert = (
        can_manage and parent is not None and _can_update(access, parent)
        and access.allows(ARCHIVE, _context(task.project, task.department))
    )
    reason = _reverse_conflict(task, parent) if can_convert else None
    items = ChecklistItem.objects.filter(
        organization=access.organization, task=task, archived_at__isnull=True,
    ).select_related("completed_by__user").order_by("position", "id")
    checklists = task.checklists.filter(organization=access.organization, archived_at__isnull=True)
    checklists = checklists.prefetch_related(Prefetch("items", queryset=items, to_attr="active_items")).order_by("id")
    conversions = task.child_conversions.filter(organization=access.organization).select_related(
        "item", "task", "actor__user",
    ).order_by("-id")
    return {
        "can_manage": can_manage,
        "can_create_subtask": can_manage and _can_create(access, task.project, task.department),
        "can_move": can_move,
        "can_convert_to_item": can_convert and reason is None,
        "convert_to_item_reason": reason,
        "checklists": checklists,
        "conversions": conversions,
    }


def _checklist(task, checklist_id):
    return get_object_or_404(
        task.checklists, organization_id=task.organization_id,
        pk=checklist_id, archived_at__isnull=True,
    )


def _items(checklist):
    return checklist.items.filter(
        organization_id=checklist.organization_id, task_id=checklist.task_id, archived_at__isnull=True,
    )


def _item(checklist, item_id):
    return get_object_or_404(_items(checklist).select_related("completed_by__user"), pk=item_id)


def _append_item(checklist, name):
    last_position = _items(checklist).aggregate(last=Max("position"))["last"] or 0
    return checklist.items.create(
        organization_id=checklist.organization_id, task_id=checklist.task_id,
        name=name, position=last_position + 1,
    )


@transaction.atomic
def create_checklist(*, user, org_id, task_id, name):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    return task.checklists.create(organization=access.organization, name=name)


@transaction.atomic
def rename_checklist(*, user, org_id, task_id, checklist_id, name):
    _, task = _managed_task(user, org_id, task_id, UPDATE)
    checklist = _checklist(task, checklist_id)
    checklist.name = name
    checklist.save(update_fields=["name"])
    return checklist


@transaction.atomic
def archive_checklist(*, user, org_id, task_id, checklist_id):
    _, task = _managed_task(user, org_id, task_id, UPDATE)
    checklist = _checklist(task, checklist_id)
    checklist.archived_at = timezone.now()
    _items(checklist).update(archived_at=checklist.archived_at)
    checklist.save(update_fields=["archived_at"])


@transaction.atomic
def create_item(*, user, org_id, task_id, checklist_id, name):
    _, task = _managed_task(user, org_id, task_id, UPDATE)
    return _append_item(_checklist(task, checklist_id), name)


@transaction.atomic
def update_item(*, user, org_id, task_id, checklist_id, item_id, changes):
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    item = _item(_checklist(task, checklist_id), item_id)
    fields = []
    if "name" in changes:
        item.name = changes["name"]
        fields.append("name")
    if "is_completed" in changes and changes["is_completed"] != item.is_completed:
        item.completed_at = timezone.now() if changes["is_completed"] else None
        item.completed_by = access.membership if changes["is_completed"] else None
        fields.extend(["completed_at", "completed_by"])
    if fields:
        item.save(update_fields=fields)
    return item


@transaction.atomic
def archive_item(*, user, org_id, task_id, checklist_id, item_id):
    _, task = _managed_task(user, org_id, task_id, UPDATE)
    item = _item(_checklist(task, checklist_id), item_id)
    item.archived_at = timezone.now()
    item.save(update_fields=["archived_at"])


@transaction.atomic
def reorder_items(*, user, org_id, task_id, checklist_id, item_ids):
    _, task = _managed_task(user, org_id, task_id, UPDATE)
    items = {item.pk: item for item in _items(_checklist(task, checklist_id))}
    if len(item_ids) != len(items) or set(item_ids) != items.keys():
        raise TaskConflict("Передайте все действующие пункты чек-листа по одному разу.")
    if not items:
        return
    ordered = [items[item_id] for item_id in item_ids]
    offset = max(item.position for item in ordered)
    for position, item in enumerate(ordered, start=1):
        item.position = offset + position
    ChecklistItem.objects.bulk_update(ordered, ["position"])
    for position, item in enumerate(ordered, start=1):
        item.position = position
    ChecklistItem.objects.bulk_update(ordered, ["position"])


def _require_confirmation(confirm):
    if confirm is not True:
        raise TaskValidation("Подтвердите преобразование.")


@transaction.atomic
def convert_item_to_task(*, user, org_id, task_id, checklist_id, item_id, confirm):
    _require_confirmation(confirm)
    access, parent = _managed_task(user, org_id, task_id, UPDATE)
    item = _item(_checklist(parent, checklist_id), item_id)
    child = create_task(user=user, org_id=org_id, name=item.name, parent_id=parent.pk)
    item.archived_at = timezone.now()
    item.save(update_fields=["archived_at"])
    ChecklistConversion.objects.create(
        organization=access.organization, parent_task=parent, task=child, item=item,
        actor=access.membership, direction=ChecklistConversion.Direction.ITEM_TO_TASK,
    )
    _audit_task(
        user, child, "checklist_converted", force=True,
        metadata={
            **_related_audit_scope(parent), "kind": ChecklistConversion.Direction.ITEM_TO_TASK,
            "checklist_id": checklist_id, "item_id": item.pk, "parent_id": parent.pk,
        },
    )
    return child


@transaction.atomic
def convert_task_to_item(*, user, org_id, task_id, checklist_id, confirm):
    _require_confirmation(confirm)
    access, task = _managed_task(user, org_id, task_id, UPDATE)
    if not access.allows(ARCHIVE, _context(task.project, task.department)):
        raise PermissionDenied
    parent = _parent_for_change(access, task.parent_id, require_writable=True)
    if parent is None:
        raise TaskConflict("Преобразовать в пункт можно только подзадачу.")
    checklist = _checklist(parent, checklist_id)
    conflict = _reverse_conflict(task, parent)
    if conflict is not None:
        raise TaskConflict(f"{conflict} Оставьте её полноценной задачей.")
    item = _append_item(checklist, task.name)
    task.is_archived = True
    task.save(update_fields=["is_archived", "updated_at"])
    ChecklistConversion.objects.create(
        organization=access.organization, parent_task=parent, task=task, item=item,
        actor=access.membership, direction=ChecklistConversion.Direction.TASK_TO_ITEM,
    )
    _audit_task(
        user, task, "checklist_converted", before={"is_archived": False}, fields=("is_archived",),
        metadata={
            **_related_audit_scope(parent), "kind": ChecklistConversion.Direction.TASK_TO_ITEM,
            "checklist_id": checklist_id, "item_id": item.pk, "parent_id": parent.pk,
        },
    )
    return item
