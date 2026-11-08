from collections import defaultdict
from urllib.parse import urlencode

from django.db.models import Exists, F, OuterRef, Subquery
from django.urls import reverse
from django.utils import timezone

from apps.access.policy import Access
from apps.organizations.models import OrganizationInvitation, OrganizationMembership

TITLES = {
    "task.assigned": "Новое поручение",
    "task.collaborator_added": "Вы добавлены как соисполнитель",
    "task.unassigned": "Вы сняты с исполнения",
    "task.reminder": "Приближается срок задачи",
    "task.overdue": "Задача просрочена",
    "task.distribution_requested": "Задача ожидает распределения",
    "review.submitted": "Результат отправлен на проверку",
    "review.requested": "Требуется проверка",
    "review.approved": "Результат принят",
    "review.returned": "Результат возвращён на доработку",
    "staffing.requested": "Запрос специалистов",
    "staffing.partial": "Запрос выполнен частично",
    "staffing.fulfilled": "Запрос выполнен",
    "staffing.rejected": "Запрос отклонён",
    "project.allocated": "Вы добавлены в проект",
    "project.invited": "Приглашение в проект",
    "project.invitation_accepted": "Приглашение в проект принято",
    "project.invitation_declined": "Приглашение в проект отклонено",
    "calendar.invited": "Приглашение на мероприятие",
    "calendar.reminder": "Скоро начнётся мероприятие",
    "calendar.rescheduled": "Время события изменилось",
    "calendar.occurrence_rescheduled": "Отдельная встреча перенесена",
    "calendar.cancelled": "Событие отменено",
    "calendar.occurrence_cancelled": "Отдельная встреча отменена",
    "calendar.series_stopped": "Будущие встречи серии остановлены",
    "organization.invited": "Приглашение в организацию",
    "department.invited": "Приглашение в подразделение",
}


def _task_targets(access, ids):
    from apps.tasks.distribution import _can_read, _distribution_rows
    from apps.tasks.models import Task, TaskDistribution, TaskReviewSubmission
    from apps.tasks.reviews import _can_read_stage, _review_stage_rows
    from apps.tasks.services import _task_rows, _task_visible

    tasks = {task.pk: task for task in _task_rows(access).filter(pk__in=ids).prefetch_related(None)}
    visible = {pk for pk, task in tasks.items() if _task_visible(access, task)}
    other = tasks.keys() - visible
    if other:
        pending = _distribution_rows(access).filter(
            task_id__in=other, status=TaskDistribution.Status.PENDING, department__is_archived=False,
            task__is_archived=False, task__status=Task.Status.PENDING, task__assignee__isnull=True,
        ).exclude(task__project__is_archived=True)
        visible.update(row.task_id for row in pending if _can_read(access, row.department))
        latest = TaskReviewSubmission.objects.filter(
            organization=access.organization, task_id=OuterRef("task_id"),
        ).order_by("-id").values("id")[:1]
        stages = _review_stage_rows(access).filter(
            task_id__in=other, task__status=Task.Status.REVIEW, task__is_archived=False, decision__isnull=True,
        ).exclude(task__project__is_archived=True).annotate(_latest_submission=Subquery(latest)).filter(
            submission_id=F("_latest_submission"),
        ).order_by("task_id", "position").distinct("task_id")
        visible.update(stage.task_id for stage in stages if _can_read_stage(access, stage))
    return {
        pk: (tasks[pk].name, reverse("organization-task", args=[access.organization.pk, pk]))
        for pk in visible
    }


def _staffing_targets(access, ids):
    from apps.projects.models import ProjectMembership
    from apps.projects.staffing import _department_side, can_request_staffing

    rows = access.organization.staffing_requests.filter(pk__in=ids).select_related(
        "project__responsible_department", "department",
    ).annotate(_active_team=Exists(ProjectMembership.objects.filter(
        organization=access.organization, project_id=OuterRef("project_id"),
        organization_membership=access.membership, is_active=True,
    )))
    targets = {}
    for row in rows:
        row.project._active_team_member = row._active_team
        if can_request_staffing(access, row.project) or _department_side(access, row):
            targets[row.pk] = (
                row.project.name, reverse("organization-staffing-request", args=[access.organization.pk, row.pk]),
            )
    return targets


def _project_targets(access, ids):
    from apps.projects.services import _can_view, _projects

    return {
        project.pk: (project.name, reverse("organization-project", args=[access.organization.pk, project.pk]))
        for project in _projects(access).filter(pk__in=ids).prefetch_related(None)
        if _can_view(access, project)
    }


def _project_invitation_targets(access, ids):
    from apps.projects.models import StaffingAllocation
    from apps.projects.staffing import own_invitations

    return {
        row.pk: (row.project.name, reverse("project-invitations"))
        for row in own_invitations(user=access.user).filter(
            organization=access.organization, pk__in=ids, status=StaffingAllocation.Status.PENDING,
            project__is_archived=False,
        )
    }


def _department_invitation_targets(access, ids):
    from apps.departments.models import DepartmentInvitation

    return {
        row.pk: (row.department.name, reverse("department-invitations"))
        for row in DepartmentInvitation.objects.filter(
            organization=access.organization, organization_membership=access.membership,
            pk__in=ids, status=DepartmentInvitation.Status.PENDING, department__is_archived=False,
        ).select_related("department")
    }


def _calendar_targets(access, ids):
    from apps.calendar_app.invitation_services import _recipient_invitations

    return {
        row.event_id: (
            row.event.title,
            reverse("organization-calendar-invitation", args=[access.organization.pk, row.pk]),
        )
        for row in _recipient_invitations(access).filter(event_id__in=ids).select_related("event")
    }


RESOLVERS = {
    "task": _task_targets,
    "staffing_request": _staffing_targets,
    "project": _project_targets,
    "project_invitation": _project_invitation_targets,
    "department_invitation": _department_invitation_targets,
    "calendar_event": _calendar_targets,
}


def presentation(user, events):
    groups = defaultdict(list)
    organizations = {}
    for event in events:
        organizations[event.organization_id] = event.organization
        groups[event.organization_id, event.object_type].append(event)
    memberships = {
        row.organization_id: row for row in OrganizationMembership.objects.filter(
            user=user, organization_id__in=organizations, is_active=True,
        )
    }
    access_by_org = {}
    for org_id, membership in memberships.items():
        access = Access(user, organizations[org_id])
        access.membership = membership
        access_by_org[org_id] = access

    invite_ids = {event.object_id for event in events if event.object_type == "organization_invitation"}
    org_invites = {
        (row.organization_id, row.pk)
        for row in OrganizationInvitation.objects.filter(
            pk__in=invite_ids, recipient=user, status=OrganizationInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        )
    }
    result = {}
    for (org_id, object_type), entries in groups.items():
        organization = organizations[org_id]
        access = access_by_org.get(org_id)
        resolver = RESOLVERS.get(object_type)
        targets = resolver(access, {event.object_id for event in entries}) if access and resolver else {}
        for event in entries:
            target = targets.get(event.object_id)
            if (
                object_type == "organization_invitation" and event.kind == "organization.invited"
                and (org_id, event.object_id) in org_invites
            ):
                target = organization.name, reverse("organization-invitations")
            if target is None:
                continue
            description, url = target
            if object_type == "calendar_event":
                key = event.data.get("occurrence_key")
                if key:
                    url += "?" + urlencode({"occurrence": key})
                if event.kind in {"calendar.rescheduled", "calendar.occurrence_rescheduled"}:
                    description += " · Проверьте новое время и подтвердите ответ, если требуется."
            result[event.pk] = {
                "title": TITLES.get(event.kind, "Уведомление"),
                "description": description,
                "organization_name": organization.name,
                "url": url,
            }
    return result
