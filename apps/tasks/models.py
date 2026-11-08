from django.db import models
from django.db.models import F, Q

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.projects.models import Project

TASK_NAME_MAX_LENGTH = 200


class Task(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        IN_PROGRESS = "in_progress", "В работе"
        REVIEW = "review", "На проверке"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    class Priority(models.TextChoices):
        LOW = "low", "Низкий"
        NORMAL = "normal", "Обычный"
        HIGH = "high", "Высокий"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=TASK_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="authored_tasks",
    )
    assignee = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="assigned_tasks",
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    planned_start_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=11, choices=Status, default=Status.PENDING)
    priority = models.CharField(max_length=6, choices=Priority, default=Priority.NORMAL)
    is_archived = models.BooleanField(default=False)
    review_policy = models.CharField(
        max_length=18, choices=Organization.TaskReviewPolicy, null=True, blank=True,
    )
    reviewer = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="explicitly_reviewed_tasks",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="unique_task_organization_id"),
            models.CheckConstraint(
                condition=Q(planned_start_at__isnull=True) | Q(due_at__isnull=True)
                | Q(due_at__gte=F("planned_start_at")),
                name="task_due_not_before_start",
            ),
            models.CheckConstraint(
                condition=Q(status="completed", completed_at__isnull=False)
                | (~Q(status="completed") & Q(completed_at__isnull=True)),
                name="task_completed_timestamp_matches_status",
            ),
        ]


class TaskCollaborator(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="task_collaborators",
    )
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="collaborators")
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="collaborating_tasks",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "organization_membership"], name="unique_task_collaborator",
            ),
        ]


class TaskDistribution(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает распределения"
        ASSIGNED = "assigned", "Назначено"
        CANCELLED = "cancelled", "Отменено"
        CLEARED = "cleared", "Назначение снято"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="distributions")
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    requested_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="requested_task_distributions",
    )
    assignee = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="received_task_distributions",
    )
    resolved_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="resolved_task_distributions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    mode = models.CharField(max_length=10, choices=Organization.TaskAssignmentMode)
    status = models.CharField(max_length=9, choices=Status, default=Status.PENDING)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task"], condition=Q(status="pending"), name="unique_pending_task_distribution",
            ),
        ]


class TaskReviewSubmission(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="review_submissions")
    submitted_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="submitted_task_reviews",
    )
    policy = models.CharField(max_length=18, choices=Organization.TaskReviewPolicy)
    comment = models.TextField(blank=True, default="")
    links = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_review_submission_scope",
            ),
        ]


class TaskReviewStage(models.Model):
    class Kind(models.TextChoices):
        DEPARTMENT = "department", "Руководитель подразделения"
        PROJECT = "project", "Руководитель проекта"
        EXPLICIT = "explicit", "Назначенный проверяющий"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    submission = models.ForeignKey(TaskReviewSubmission, on_delete=models.CASCADE, related_name="stages")
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=10, choices=Kind)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    reviewer = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "position"], name="unique_task_review_stage_position"),
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_review_stage_scope",
            ),
            models.CheckConstraint(condition=Q(position__gt=0), name="task_review_stage_position_positive"),
        ]


class TaskReviewDecision(models.Model):
    class Outcome(models.TextChoices):
        APPROVED = "approved", "Принято"
        RETURNED = "returned", "Возвращено на доработку"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    stage = models.OneToOneField(TaskReviewStage, on_delete=models.CASCADE, related_name="decision")
    actor = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=8, choices=Outcome)
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
