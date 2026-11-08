from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership

PROJECT_NAME_MAX_LENGTH = 200


class Project(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активный"
        PAUSED = "paused", "Приостановлен"
        COMPLETED = "completed", "Завершён"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=PROJECT_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    lead_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="led_projects",
    )
    responsible_department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True,
        related_name="responsible_projects",
    )
    start_date = models.DateField(null=True, blank=True)
    planned_end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_projects",
    )
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(planned_end_date__isnull=True)
                | Q(planned_end_date__gte=F("start_date")),
                name="project_planned_end_not_before_start",
            ),
            models.UniqueConstraint(fields=["organization", "id"], name="unique_project_organization_id"),
        ]

    def __str__(self):
        return self.name


class ProjectDepartmentLink(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="project_department_links",
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="department_links")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="participating_project_links",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "department"], name="unique_project_participating_department",
            ),
        ]
