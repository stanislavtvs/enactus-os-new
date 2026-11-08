from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, OuterRef, Prefetch, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.policy import Access
from apps.access.services import organization_access
from apps.organizations.models import Organization
from apps.projects.services import _can_view as can_view_project
from apps.projects.services import _context as project_context

from .distribution import pending_distribution_for_reader
from .models import Task, TaskReviewDecision, TaskReviewStage, TaskReviewSubmission
from .services import (
    TERMINAL_STATUSES,
    TaskConflict,
    TaskValidation,
    _assignee_is_active,
    _context,
    _has_pending_distribution,
    _managed_task,
    _task_is_writable,
    _task_rows,
    _task_visible,
    has_blocking_dependencies,
    require_no_blocking_dependencies,
)

SUBMIT = "tasks.review.submit"
APPROVE = "tasks.review.approve"
RETURN = "tasks.review.return"
MANAGE = "tasks.review.manage"


def effective_policy(task):
    policy = task.review_policy
    if policy is None and task.project_id is not None:
        policy = task.project.task_review_policy
    return str(policy or task.organization.task_review_policy)


def can_manage_organization_review(access):
    return access.allows(MANAGE)


def can_manage_project_review(access, project):
    return (
        not project.is_archived and can_view_project(access, project)
        and access.allows(MANAGE, project_context(project))
    )


def can_manage_task_review(access, task):
    return (
        _task_visible(access, task) and _task_is_writable(task)
        and task.status not in TERMINAL_STATUSES | {Task.Status.REVIEW}
        and access.allows(MANAGE, _context(task.project, task.department))
    )


def _validate_policy(policy, *, allow_inherit=False):
    if allow_inherit and policy is None:
        return
    if policy not in Organization.TaskReviewPolicy.values:
        raise TaskValidation("Выберите допустимый маршрут проверки.")


@transaction.atomic
def set_organization_review_policy(*, user, org_id, policy):
    access = organization_access(user=user, org_id=org_id, lock=True)
    if not can_manage_organization_review(access):
        raise PermissionDenied
    _validate_policy(policy)
    organization = access.organization
    organization.task_review_policy = policy
    organization.save(update_fields=["task_review_policy"])
    return organization


@transaction.atomic
def set_project_review_policy(*, user, org_id, project_id, policy):
    access = organization_access(user=user, org_id=org_id, lock=True)
    project = get_object_or_404(
        access.organization.projects.select_for_update(of=("self",)).select_related("responsible_department"),
        pk=project_id,
    )
    if not can_view_project(access, project):
        raise Http404
    if not access.allows(MANAGE, project_context(project)):
        raise PermissionDenied
    if project.is_archived:
        raise TaskConflict("Маршрут архивного проекта нельзя изменить.")
    _validate_policy(policy, allow_inherit=True)
    project.task_review_policy = policy
    project.save(update_fields=["task_review_policy", "updated_at"])
    return project


def _can_be_reviewer(access, task, member):
    if member.pk == task.assignee_id or (not access.is_owner and member.pk == access.membership.pk):
        return False
    return Access(member.user, access.organization).allows(APPROVE, _context(task.project, task.department))


def review_candidates(access, task):
    if task.project_id is not None or not can_manage_task_review(access, task):
        return []
    return [
        member for member in access.organization.memberships.filter(
            is_active=True, user__is_active=True,
        ).select_related("user").order_by("id")
        if _can_be_reviewer(access, task, member)
    ]


@transaction.atomic
def set_task_review_policy(*, user, org_id, task_id, policy, reviewer_id=None):
    access, task = _managed_task(user, org_id, task_id, MANAGE)
    if task.status in TERMINAL_STATUSES | {Task.Status.REVIEW}:
        raise TaskConflict("Маршрут нельзя изменить во время проверки или после закрытия задачи.")
    _validate_policy(policy, allow_inherit=True)
    reviewer = None
    if reviewer_id is not None:
        if task.project_id is not None:
            raise TaskConflict("Явный проверяющий доступен только для задачи без проекта.")
        reviewer = get_object_or_404(
            access.organization.memberships.select_related("user"),
            pk=reviewer_id, is_active=True, user__is_active=True,
        )
        if not _can_be_reviewer(access, task, reviewer):
            raise TaskConflict("Участник не может быть назначен проверяющим этой задачи.")
    task.review_policy = policy
    task.reviewer = reviewer
    task.save(update_fields=["review_policy", "reviewer", "updated_at"])
    return task


def can_submit(access, task):
    return (
        access.membership is not None and task.assignee_id == access.membership.pk
        and _task_visible(access, task) and _task_is_writable(task)
        and task.status in (Task.Status.PENDING, Task.Status.IN_PROGRESS)
        and _assignee_is_active(task) and not _has_pending_distribution(task)
        and not has_blocking_dependencies(task)
        and access.allows(SUBMIT, _context(task.project, task.department))
    )


def _stage_specs(task, policy):
    department = _context(task.project, task.department)
    if task.project_id is None and task.reviewer_id is not None:
        reviewer = task.reviewer
        if (
            reviewer.pk == task.assignee_id
            or not Access(reviewer.user, task.organization).allows(APPROVE, department)
        ):
            raise TaskConflict("Назначенный проверяющий больше недоступен.")
        return [(TaskReviewStage.Kind.EXPLICIT, department, reviewer)]

    specs = []
    if policy in (Organization.TaskReviewPolicy.DEPARTMENT, Organization.TaskReviewPolicy.DEPARTMENT_PROJECT):
        if department is None:
            raise TaskConflict("Для проверки подразделением укажите действующее подразделение.")
        specs.append((TaskReviewStage.Kind.DEPARTMENT, department, None))
    if policy in (Organization.TaskReviewPolicy.PROJECT, Organization.TaskReviewPolicy.DEPARTMENT_PROJECT):
        if task.project_id is None:
            raise TaskConflict("Для задачи без проекта выберите подразделение или назначенного проверяющего.")
        lead = task.organization.memberships.filter(
            pk=task.project.lead_membership_id, is_active=True, user__is_active=True,
        ).select_related("user").first()
        department = project_context(task.project)
        if (
            lead is None or lead.pk == task.assignee_id
            or not Access(lead.user, task.organization).allows(APPROVE, department)
        ):
            raise TaskConflict("Для проверки нужен другой действующий руководитель проекта.")
        specs.append((TaskReviewStage.Kind.PROJECT, department, None))
    return specs


def _review_stage_rows(access):
    return TaskReviewStage.objects.filter(organization=access.organization).select_related(
        "task__project__responsible_department", "task__department", "department", "reviewer__user",
        "submission__submitted_by__user", "decision__actor__user",
    )


def _review_submissions(access, task):
    stages = _review_stage_rows(access).order_by("position")
    return task.review_submissions.filter(organization=access.organization).select_related(
        "submitted_by__user",
    ).prefetch_related(Prefetch("stages", queryset=stages)).order_by("-id")


def _current_stage(access, task):
    if task.status != Task.Status.REVIEW or not _task_is_writable(task):
        return None
    latest = task.review_submissions.filter(organization=access.organization).order_by("-id").values("id")[:1]
    return _review_stage_rows(access).filter(
        task=task, submission_id=Subquery(latest), decision__isnull=True,
    ).order_by("position").first()


def can_decide(access, stage, outcome):
    if stage is None or access.membership is None:
        return False
    task = stage.task
    if (
        stage.organization_id != access.organization.pk
        or task.status != Task.Status.REVIEW or not _task_is_writable(task)
        or access.membership.pk in (stage.submission.submitted_by_id, task.assignee_id)
    ):
        return False

    if outcome == TaskReviewDecision.Outcome.APPROVED:
        action = APPROVE
    elif outcome == TaskReviewDecision.Outcome.RETURNED:
        action = RETURN
    else:
        return False

    if stage.kind == TaskReviewStage.Kind.DEPARTMENT:
        if stage.department.is_archived:
            return outcome == TaskReviewDecision.Outcome.RETURNED and access.is_owner
        return access.allows(action, stage.department)
    if stage.kind == TaskReviewStage.Kind.PROJECT:
        if task.project_id is None or task.project.lead_membership_id != access.membership.pk:
            return False
    elif stage.kind != TaskReviewStage.Kind.EXPLICIT or stage.reviewer_id != access.membership.pk:
        return False
    department = stage.department if stage.department_id and not stage.department.is_archived else None
    return access.allows(action, department)


def _can_read_stage(access, stage):
    return any(can_decide(access, stage, outcome) for outcome in TaskReviewDecision.Outcome)


def get_review(*, user, org_id, task_id):
    access = organization_access(user=user, org_id=org_id)
    task = get_object_or_404(_task_rows(access), pk=task_id)
    stage = _current_stage(access, task)
    submissions = _review_submissions(access, task)
    if _task_visible(access, task) or _can_read_stage(access, stage):
        return task, submissions, stage
    if pending_distribution_for_reader(access, task) is not None:
        return task, submissions.none(), None
    raise Http404


def visible_workflow_task(*, access, task_id):
    task = get_object_or_404(_task_rows(access), pk=task_id)
    if (
        _task_visible(access, task)
        or pending_distribution_for_reader(access, task) is not None
        or _can_read_stage(access, _current_stage(access, task))
    ):
        return task
    raise Http404


def can_open_review_inbox(access):
    if access.allows(APPROVE) or access.allows(RETURN):
        return True
    return any(
        access.allows(APPROVE, department) or access.allows(RETURN, department)
        for department in access.organization.departments.filter(is_archived=False)
    )


def review_inbox(*, user, org_id):
    access = organization_access(user=user, org_id=org_id)
    latest = TaskReviewSubmission.objects.filter(
        organization=access.organization, task_id=OuterRef("task_id"),
    ).order_by("-id").values("id")[:1]
    stages = _review_stage_rows(access).filter(
        task__status=Task.Status.REVIEW, task__is_archived=False, decision__isnull=True,
    ).exclude(task__project__is_archived=True).annotate(_latest_submission=Subquery(latest)).filter(
        submission_id=F("_latest_submission"),
    ).order_by("task_id", "position").distinct("task_id")
    return [stage for stage in stages if _can_read_stage(access, stage)]


@transaction.atomic
def submit_review(*, user, org_id, task_id, comment, links):
    access, task = _managed_task(user, org_id, task_id, SUBMIT)
    if task.assignee_id != access.membership.pk:
        raise PermissionDenied
    require_no_blocking_dependencies(task)
    if not can_submit(access, task):
        raise TaskConflict("Отправить результат может действующий исполнитель открытой задачи.")
    policy = effective_policy(task)
    specs = _stage_specs(task, policy)
    if not specs:
        raise TaskConflict("Для задачи не задан допустимый маршрут проверки.")

    submission = task.review_submissions.create(
        organization=access.organization, submitted_by=access.membership,
        comment=comment, links=links, policy=policy,
    )
    TaskReviewStage.objects.bulk_create([
        TaskReviewStage(
            organization=access.organization, task=task, submission=submission,
            position=position, kind=kind, department=department, reviewer=reviewer,
        )
        for position, (kind, department, reviewer) in enumerate(specs, start=1)
    ])
    task.status = Task.Status.REVIEW
    task.save(update_fields=["status", "updated_at"])
    return submission


@transaction.atomic
def decide_review(*, user, org_id, task_id, stage_id, outcome, comment):
    access = organization_access(user=user, org_id=org_id, lock=True)
    task = get_object_or_404(_task_rows(access, lock=True), pk=task_id)
    stage = _current_stage(access, task)
    if not _task_visible(access, task) and not _can_read_stage(access, stage):
        raise Http404
    if stage is None or stage.pk != stage_id:
        raise TaskConflict("Этот этап уже изменился. Обновите маршрут проверки.")
    if not can_decide(access, stage, outcome):
        raise PermissionDenied
    if outcome == TaskReviewDecision.Outcome.RETURNED and not comment.strip():
        raise TaskValidation("Укажите причину возврата на доработку.")

    decision = TaskReviewDecision.objects.create(
        organization=access.organization, task=task, stage=stage, actor=access.membership,
        outcome=outcome, comment=comment.strip(),
    )
    if outcome == TaskReviewDecision.Outcome.RETURNED:
        task.status = Task.Status.IN_PROGRESS
        task.save(update_fields=["status", "updated_at"])
    elif not stage.submission.stages.filter(position__gt=stage.position).exists():
        task.status = Task.Status.COMPLETED
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_at", "updated_at"])
    return decision
