from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from ninja import PatchDict, Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access
from apps.projects.services import visible_project

from . import distribution, reviews, services
from .schemas import (
    AssigneeInput,
    RecordId,
    ReviewDecisionInput,
    ReviewPolicyInput,
    ReviewPolicyOutput,
    ReviewSubmissionInput,
    StatusInput,
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
        "distribution_pending": task._distribution_pending,
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
    query = filters.model_dump(exclude={"queue"})
    queue_ids = None
    if filters.queue == "distribution":
        queue_ids = [entry.task_id for entry in distribution.distribution_inbox(user=request.user, org_id=org_id)]
    elif filters.queue == "review":
        queue_ids = [stage.task_id for stage in reviews.review_inbox(user=request.user, org_id=org_id)]
    tasks, total = services.list_tasks(access=access, queue_task_ids=queue_ids, **query)
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
