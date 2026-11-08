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
