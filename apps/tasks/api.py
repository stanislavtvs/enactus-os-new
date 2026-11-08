from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from ninja import PatchDict, Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access
from apps.projects.services import visible_project

from . import decomposition, dependencies, distribution, reviews, services
from .models import Task
from .schemas import (
    AssigneeInput,
    ChecklistItemChangeInput,
    ChecklistItemOutput,
    ChecklistNameInput,
    ChecklistOrderInput,
    ChecklistOutput,
    ConversionInput,
    DecompositionOutput,
    DependencyInput,
    DependencyListOutput,
    DependencyOutput,
    HierarchyQuery,
    InverseConversionInput,
    ParentInput,
    RecordId,
    ReviewDecisionInput,
    ReviewPolicyInput,
    ReviewPolicyOutput,
    ReviewSubmissionInput,
    StatusInput,
    SubtaskInput,
    SubtaskListOutput,
    TaskChangeInput,
    TaskInput,
    TaskListOutput,
    TaskListQuery,
    TaskOptions,
    TaskOutput,
    TaskReviewPolicyInput,
    WorkflowOutput,
)

router = Router(auth=django_auth)
TASK_CONFLICT_CONSTRAINTS = {
    "task_participant_requires_active_org_member",
    "task_participant_requires_active_project_member",
    "task_participant_roles_distinct",
    "unique_task_collaborator",
    "project_requires_active_department",
    "task_parent_is_not_self",
    "task_parent_tree_no_cycle",
    "task_new_parent_must_be_active",
    "checklist_conversion_task_parent_matches",
    "checklist_conversion_source_archived",
    "unique_active_checklist_item_position",
    "unique_checklist_item_forward_conversion",
    "unique_task_inverse_conversion",
    "task_dependency_is_not_self",
    "unique_task_dependency_pair",
    "task_dependency_graph_no_cycle",
    "task_strict_dependencies_block_start",
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
    return {"id": member.pk, "username": member.user.get_username()} if member is not None else None


def _task_data(task, access, parents=None):
    if parents is None:
        parents = services.visible_parent_names(access, [task])
    parent_id = task.parent_id if task.parent_id in parents else None
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
        "distribution_pending": task._distribution_pending,
        "author": _member_data(task.created_by),
        "assignee": _member_data(task.assignee) if task.assignee_id is not None else None,
        "collaborators": [_member_data(link.organization_membership) for link in task.active_collaborators],
        "project": task.project,
        "department": task.department,
        "parent_id": parent_id,
        "parent": {"id": parent_id, "name": parents[parent_id]} if parent_id is not None else None,
        "has_unfinished_children": task.has_unfinished_children,
        "can_view_decomposition": services._task_visible(access, task),
        "has_blocking_dependencies": services.has_blocking_dependencies(task),
        "has_dependency_warnings": services.has_dependency_warnings(task),
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
    query = filters.model_dump(exclude={"queue"})
    queue_ids = None
    if filters.queue == "distribution":
        queue_ids = [entry.task_id for entry in distribution.distribution_inbox(user=request.user, org_id=org_id)]
    elif filters.queue == "review":
        queue_ids = [stage.task_id for stage in reviews.review_inbox(user=request.user, org_id=org_id)]
    tasks, total = services.list_tasks(access=access, queue_task_ids=queue_ids, **query)
    tasks = list(tasks)
    parents = services.visible_parent_names(access, tasks)
    return {
        "items": [_task_data(task, access, parents) for task in tasks],
        "total": total,
        "page": filters.page,
        "page_size": filters.page_size,
    }


@router.post("/{org_id}/tasks/", response={201: TaskOutput})
def create_task(request, org_id: RecordId, payload: TaskInput):
    return 201, _changed_task(
        request, org_id, services.create_task, **payload.model_dump(exclude_unset=True),
    )


@router.get("/{org_id}/tasks/{task_id}/", response=TaskOutput, url_name="task_detail")
def get_task(request, org_id: RecordId, task_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    return _task_data(reviews.visible_workflow_task(access=access, task_id=task_id), access)


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


def _distribution_data(entry):
    return {
        "status": entry.status,
        "mode": entry.mode,
        "department": entry.department,
        "assignee": _member_data(entry.assignee),
        "requested_by": _member_data(entry.requested_by),
        "resolved_by": _member_data(entry.resolved_by),
        "created_at": entry.created_at,
        "resolved_at": entry.resolved_at,
    }


def _decision_data(stage):
    decision = getattr(stage, "decision", None)
    if decision is None:
        return None
    return {
        "outcome": decision.outcome,
        "comment": decision.comment,
        "actor": _member_data(decision.actor),
        "created_at": decision.created_at,
    }


def _workflow_command(request, org_id, operation, **kwargs):
    _perform(operation, user=request.user, org_id=org_id, **kwargs)
    return 204, None


@router.get("/{org_id}/tasks/{task_id}/workflow/", response=WorkflowOutput)
def task_workflow(request, org_id: RecordId, task_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    task, submissions, current_stage = _perform(
        reviews.get_review, user=request.user, org_id=org_id, task_id=task_id,
    )
    history = list(distribution.distribution_history(access=access, task=task))
    pending = next((entry for entry in history if entry.status == "pending"), None)
    can_allocate = pending is not None and distribution.can_distribute(access, pending)
    can_manage = reviews.can_manage_task_review(access, task)
    return {
        "distribution": {
            "pending": pending is not None,
            "can_route": distribution.can_route(access, task),
            "can_allocate": can_allocate,
            "candidates": [
                _member_data(member) for member in distribution.distribution_candidates(
                    user=request.user, org_id=org_id, task_id=task_id,
                )
            ] if can_allocate else [],
            "history": [_distribution_data(entry) for entry in history],
        },
        "review": {
            "policy": reviews.effective_policy(task),
            "override": task.review_policy,
            "reviewer_id": task.reviewer_id,
            "can_manage": can_manage,
            "reviewers": [
                _member_data(member) for member in reviews.review_candidates(access, task)
            ] if can_manage else [],
            "can_submit": reviews.can_submit(access, task),
            "can_approve": current_stage is not None and reviews.can_decide(access, current_stage, "approved"),
            "can_return": current_stage is not None and reviews.can_decide(access, current_stage, "returned"),
            "current_stage_id": current_stage.pk if current_stage is not None else None,
            "current_stage_kind": current_stage.kind if current_stage is not None else None,
            "submissions": [
                {
                    "id": submission.pk,
                    "comment": submission.comment,
                    "links": submission.links,
                    "created_at": submission.created_at,
                    "submitted_by": _member_data(submission.submitted_by),
                    "stages": [
                        {
                            "id": stage.pk,
                            "kind": stage.kind,
                            "position": stage.position,
                            "decision": _decision_data(stage),
                        }
                        for stage in submission.stages.all()
                    ],
                }
                for submission in submissions
            ],
        },
    }


@router.post("/{org_id}/tasks/{task_id}/distribution/", response={204: None})
def route_task(request, org_id: RecordId, task_id: RecordId):
    return _workflow_command(request, org_id, distribution.route_task, task_id=task_id)


@router.put("/{org_id}/tasks/{task_id}/distribution/", response={204: None})
def distribute_task(request, org_id: RecordId, task_id: RecordId, payload: AssigneeInput):
    if payload.assignee_id is None:
        raise HttpError(422, "Выберите исполнителя.")
    return _workflow_command(
        request, org_id, distribution.distribute_task, task_id=task_id, assignee_id=payload.assignee_id,
    )


@router.post("/{org_id}/tasks/{task_id}/review/submissions/", response={204: None})
def submit_review(request, org_id: RecordId, task_id: RecordId, payload: ReviewSubmissionInput):
    return _workflow_command(
        request, org_id, reviews.submit_review, task_id=task_id,
        comment=payload.comment, links=[str(link) for link in payload.links],
    )


@router.post("/{org_id}/tasks/{task_id}/review/decisions/", response={204: None})
def decide_review(request, org_id: RecordId, task_id: RecordId, payload: ReviewDecisionInput):
    return _workflow_command(
        request, org_id, reviews.decide_review, task_id=task_id, **payload.model_dump(),
    )


@router.put("/{org_id}/tasks/{task_id}/review/policy/", response={204: None})
def task_review_policy(request, org_id: RecordId, task_id: RecordId, payload: TaskReviewPolicyInput):
    return _workflow_command(
        request, org_id, reviews.set_task_review_policy, task_id=task_id, **payload.model_dump(),
    )


@router.get("/{org_id}/task-review-policy/", response=ReviewPolicyOutput)
def organization_review_policy(request, org_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    return {
        "policy": access.organization.task_review_policy,
        "can_manage": reviews.can_manage_organization_review(access),
    }


@router.put("/{org_id}/task-review-policy/", response={204: None})
def set_organization_review_policy(request, org_id: RecordId, payload: ReviewPolicyInput):
    if payload.policy is None:
        raise HttpError(422, "Выберите маршрут проверки по умолчанию.")
    return _workflow_command(request, org_id, reviews.set_organization_review_policy, policy=payload.policy)


@router.get("/{org_id}/projects/{project_id}/task-review-policy/", response=ReviewPolicyOutput)
def project_review_policy(request, org_id: RecordId, project_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    project = visible_project(access=access, project_id=project_id)
    return {
        "policy": project.task_review_policy,
        "can_manage": reviews.can_manage_project_review(access, project),
    }


@router.put("/{org_id}/projects/{project_id}/task-review-policy/", response={204: None})
def set_project_review_policy(request, org_id: RecordId, project_id: RecordId, payload: ReviewPolicyInput):
    return _workflow_command(
        request, org_id, reviews.set_project_review_policy, project_id=project_id, policy=payload.policy,
    )


@router.get("/{org_id}/tasks/{task_id}/subtasks/", response=SubtaskListOutput)
def list_subtasks(request, org_id: RecordId, task_id: RecordId, filters: Query[HierarchyQuery]):
    access = organization_access(user=request.user, org_id=org_id)
    tasks, total, completed = decomposition.list_subtasks(
        access=access, task_id=task_id, **filters.model_dump(),
    )
    tasks = list(tasks)
    parents = services.visible_parent_names(access, tasks)
    return {
        "items": [
            {
                "id": task.pk, "name": task.name, "status": task.status,
                "parent_id": task.parent_id if task.parent_id in parents else None,
                "assignee": _member_data(task.assignee), "due_at": task.due_at,
                "is_archived": task.is_archived,
            }
            for task in tasks
        ],
        "total": total, "completed": completed, "page": filters.page, "page_size": filters.page_size,
    }


@router.post("/{org_id}/tasks/{task_id}/subtasks/", response={201: TaskOutput})
def create_subtask(request, org_id: RecordId, task_id: RecordId, payload: SubtaskInput):
    return 201, _changed_task(
        request, org_id, services.create_task, parent_id=task_id, **payload.model_dump(exclude_unset=True),
    )


@router.put("/{org_id}/tasks/{task_id}/parent/", response=TaskOutput)
def set_task_parent(request, org_id: RecordId, task_id: RecordId, payload: ParentInput):
    return _changed_task(
        request, org_id, services.set_task_parent, task_id=task_id, parent_id=payload.parent_id,
    )


def _checklist_item_data(item):
    return {
        "id": item.pk,
        "name": item.name,
        "position": item.position,
        "is_completed": item.is_completed,
        "completed_at": item.completed_at,
        "completed_by": _member_data(item.completed_by),
    }


def _checklist_data(checklist):
    items = getattr(checklist, "active_items", None)
    if items is None:
        items = checklist.items.filter(archived_at__isnull=True).select_related(
            "completed_by__user",
        ).order_by("position", "id")
    items = [_checklist_item_data(item) for item in items]
    return {
        "id": checklist.pk, "name": checklist.name, "items": items,
        "total": len(items), "completed": sum(item["is_completed"] for item in items),
    }


@router.get("/{org_id}/tasks/{task_id}/decomposition/", response=DecompositionOutput)
def task_decomposition(request, org_id: RecordId, task_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    data = decomposition.get_decomposition(access=access, task_id=task_id)
    events = list(data["conversions"])
    visible_ids = set(services._visible_rows(access).filter(
        pk__in={event.task_id for event in events},
    ).values_list("id", flat=True)) if events else set()
    return {
        **data,
        "checklists": [_checklist_data(checklist) for checklist in data["checklists"]],
        "conversions": [
            {
                "id": event.pk, "direction": event.direction,
                "item_id": event.item_id, "item_name": event.item.name,
                "task_id": event.task_id if event.task_id in visible_ids else None,
                "task_name": event.task.name if event.task_id in visible_ids else None,
                "actor": _member_data(event.actor), "created_at": event.created_at,
            }
            for event in events
        ],
    }


@router.post("/{org_id}/tasks/{task_id}/checklists/", response={201: ChecklistOutput})
def create_checklist(request, org_id: RecordId, task_id: RecordId, payload: ChecklistNameInput):
    checklist = _perform(
        decomposition.create_checklist, user=request.user, org_id=org_id, task_id=task_id,
        name=payload.name,
    )
    return 201, _checklist_data(checklist)


@router.patch("/{org_id}/tasks/{task_id}/checklists/{checklist_id}/", response=ChecklistOutput)
def rename_checklist(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, payload: ChecklistNameInput,
):
    checklist = _perform(
        decomposition.rename_checklist, user=request.user, org_id=org_id, task_id=task_id,
        checklist_id=checklist_id, name=payload.name,
    )
    return _checklist_data(checklist)


@router.delete("/{org_id}/tasks/{task_id}/checklists/{checklist_id}/", response={204: None})
def archive_checklist(request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId):
    return _workflow_command(
        request, org_id, decomposition.archive_checklist, task_id=task_id, checklist_id=checklist_id,
    )


@router.post("/{org_id}/tasks/{task_id}/checklists/{checklist_id}/items/", response={201: ChecklistItemOutput})
def create_checklist_item(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, payload: ChecklistNameInput,
):
    item = _perform(
        decomposition.create_item, user=request.user, org_id=org_id, task_id=task_id,
        checklist_id=checklist_id, name=payload.name,
    )
    return 201, _checklist_item_data(item)


@router.patch(
    "/{org_id}/tasks/{task_id}/checklists/{checklist_id}/items/{item_id}/", response=ChecklistItemOutput,
)
def update_checklist_item(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, item_id: RecordId,
    payload: PatchDict[ChecklistItemChangeInput],
):
    item = _perform(
        decomposition.update_item, user=request.user, org_id=org_id, task_id=task_id,
        checklist_id=checklist_id, item_id=item_id, changes=payload,
    )
    return _checklist_item_data(item)


@router.delete(
    "/{org_id}/tasks/{task_id}/checklists/{checklist_id}/items/{item_id}/", response={204: None},
)
def archive_checklist_item(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, item_id: RecordId,
):
    return _workflow_command(
        request, org_id, decomposition.archive_item, task_id=task_id, checklist_id=checklist_id,
        item_id=item_id,
    )


@router.put("/{org_id}/tasks/{task_id}/checklists/{checklist_id}/order/", response={204: None})
def reorder_checklist_items(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, payload: ChecklistOrderInput,
):
    return _workflow_command(
        request, org_id, decomposition.reorder_items, task_id=task_id, checklist_id=checklist_id,
        item_ids=payload.item_ids,
    )


@router.post(
    "/{org_id}/tasks/{task_id}/checklists/{checklist_id}/items/{item_id}/convert/",
    response={201: TaskOutput},
)
def convert_item_to_task(
    request, org_id: RecordId, task_id: RecordId, checklist_id: RecordId, item_id: RecordId,
    payload: ConversionInput,
):
    return 201, _changed_task(
        request, org_id, decomposition.convert_item_to_task, task_id=task_id, checklist_id=checklist_id,
        item_id=item_id, confirm=payload.confirm,
    )


@router.post("/{org_id}/tasks/{task_id}/convert-to-item/", response={201: ChecklistItemOutput})
def convert_task_to_item(request, org_id: RecordId, task_id: RecordId, payload: InverseConversionInput):
    item = _perform(
        decomposition.convert_task_to_item, user=request.user, org_id=org_id, task_id=task_id,
        checklist_id=payload.checklist_id, confirm=payload.confirm,
    )
    return 201, _checklist_item_data(item)


def _dependency_data(dependency, *, predecessor_visible):
    predecessor = dependency.predecessor
    return {
        "id": dependency.pk,
        "kind": dependency.kind,
        "predecessor": {
            "id": predecessor.pk, "name": predecessor.name, "status": predecessor.status,
        } if predecessor_visible else None,
        "unresolved": predecessor.status != Task.Status.COMPLETED,
    }


@router.get("/{org_id}/tasks/{task_id}/dependencies/", response=DependencyListOutput)
def list_task_dependencies(request, org_id: RecordId, task_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    task, can_manage, rows = dependencies.list_dependencies(access=access, task_id=task_id)
    rows = list(rows)
    visible_ids = set(services._visible_rows(access).filter(
        pk__in={dependency.predecessor_id for dependency in rows},
    ).values_list("id", flat=True)) if rows else set()
    return {
        "can_manage": can_manage,
        "blocked": services.has_blocking_dependencies(task),
        "items": [
            _dependency_data(dependency, predecessor_visible=dependency.predecessor_id in visible_ids)
            for dependency in rows
        ],
    }


@router.post("/{org_id}/tasks/{task_id}/dependencies/", response={201: DependencyOutput})
def add_task_dependency(request, org_id: RecordId, task_id: RecordId, payload: DependencyInput):
    dependency = _perform(
        dependencies.add_dependency, user=request.user, org_id=org_id, task_id=task_id,
        predecessor_id=payload.predecessor_id, kind=payload.kind,
    )
    return 201, _dependency_data(dependency, predecessor_visible=True)


@router.delete("/{org_id}/tasks/{task_id}/dependencies/{dependency_id}/", response={204: None})
def remove_task_dependency(request, org_id: RecordId, task_id: RecordId, dependency_id: RecordId):
    return _workflow_command(
        request, org_id, dependencies.remove_dependency, task_id=task_id, dependency_id=dependency_id,
    )
