from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from ninja import PatchDict, Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access

from . import services
from .schemas import (
    AssigneeInput,
    RecordId,
    StatusInput,
    TaskChangeInput,
    TaskInput,
    TaskListOutput,
    TaskListQuery,
    TaskOptions,
    TaskOutput,
)

router = Router(auth=django_auth)
TASK_CONFLICT_CONSTRAINTS = {
    "task_participant_requires_active_org_member",
    "task_participant_requires_active_project_member",
    "task_participant_roles_distinct",
    "unique_task_collaborator",
    "project_requires_active_department",
}


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except services.TaskConflict as exc:
        raise HttpError(409, str(exc)) from None
    except services.TaskValidation as exc:
        raise HttpError(422, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in TASK_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Состояние участника или связанного объекта изменилось.") from None


def _member_data(member):
    return {"id": member.pk, "username": member.user.get_username()}


def _task_data(task, access):
    return {
        "id": task.pk,
        "name": task.name,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "planned_start_at": task.planned_start_at,
        "due_at": task.due_at,
        "completed_at": task.completed_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "is_archived": task.is_archived,
        "author": _member_data(task.created_by),
        "assignee": _member_data(task.assignee) if task.assignee_id is not None else None,
        "collaborators": [_member_data(link.organization_membership) for link in task.active_collaborators],
        "project": task.project,
        "department": task.department,
        "permissions": services.task_permissions(access=access, task=task),
        "allowed_statuses": services.allowed_statuses(access=access, task=task),
    }


@transaction.atomic
def _changed_task(request, org_id, operation, **kwargs):
    task = _perform(operation, user=request.user, org_id=org_id, **kwargs)
    access = organization_access(user=request.user, org_id=org_id)
    task = services.visible_task(access=access, task_id=task.pk)
    return _task_data(task, access)


@router.get("/{org_id}/tasks/", response=TaskListOutput, url_name="task_list")
def list_tasks(request, org_id: RecordId, filters: Query[TaskListQuery]):
    access = organization_access(user=request.user, org_id=org_id)
    tasks, total = services.list_tasks(access=access, **filters.model_dump())
    return {
        "items": [_task_data(task, access) for task in tasks],
        "total": total,
        "page": filters.page,
        "page_size": filters.page_size,
    }


@router.post("/{org_id}/tasks/", response={201: TaskOutput})
def create_task(request, org_id: RecordId, payload: TaskInput):
    return 201, _changed_task(request, org_id, services.create_task, **payload.model_dump())


@router.get("/{org_id}/tasks/{task_id}/", response=TaskOutput, url_name="task_detail")
def get_task(request, org_id: RecordId, task_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    return _task_data(services.visible_task(access=access, task_id=task_id), access)


@router.patch("/{org_id}/tasks/{task_id}/", response=TaskOutput)
def update_task(request, org_id: RecordId, task_id: RecordId, payload: PatchDict[TaskChangeInput]):
    return _changed_task(request, org_id, services.update_task, task_id=task_id, changes=payload)


@router.delete("/{org_id}/tasks/{task_id}/", response={204: None})
def archive_task(request, org_id: RecordId, task_id: RecordId):
    _perform(services.archive_task, user=request.user, org_id=org_id, task_id=task_id)
    return 204, None


@router.put("/{org_id}/tasks/{task_id}/assignee/", response=TaskOutput)
def set_assignee(request, org_id: RecordId, task_id: RecordId, payload: AssigneeInput):
    return _changed_task(request, org_id, services.set_assignee, task_id=task_id, assignee_id=payload.assignee_id)


@router.put("/{org_id}/tasks/{task_id}/collaborators/{membership_id}/", response={204: None})
def add_collaborator(request, org_id: RecordId, task_id: RecordId, membership_id: RecordId):
    _perform(
        services.set_collaborator, user=request.user, org_id=org_id,
        task_id=task_id, membership_id=membership_id, include=True,
    )
    return 204, None


@router.delete("/{org_id}/tasks/{task_id}/collaborators/{membership_id}/", response={204: None})
def remove_collaborator(request, org_id: RecordId, task_id: RecordId, membership_id: RecordId):
    _perform(
        services.set_collaborator, user=request.user, org_id=org_id,
        task_id=task_id, membership_id=membership_id, include=False,
    )
    return 204, None


@router.put("/{org_id}/tasks/{task_id}/status/", response=TaskOutput)
def set_status(request, org_id: RecordId, task_id: RecordId, payload: StatusInput):
    return _changed_task(request, org_id, services.set_status, task_id=task_id, status=payload.status)


@router.get("/{org_id}/task-options/", response=TaskOptions, url_name="task_options")
def get_task_options(request, org_id: RecordId, task_id: RecordId | None = None):
    access = organization_access(user=request.user, org_id=org_id)
    return services.task_options(access=access, task_id=task_id)
