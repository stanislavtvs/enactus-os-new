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
    task_review_policy = models.CharField(
        max_length=18, choices=Organization.TaskReviewPolicy, null=True, blank=True,
    )
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


class ProjectMembership(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="project_memberships",
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="project_memberships",
    )
    source_department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True,
        related_name="sourced_project_memberships",
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "organization_membership"],
                name="unique_project_membership",
            ),
        ]


class StaffingRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SENT = "sent", "Отправлен"
        PARTIAL = "partial", "Выполнен частично"
        FULFILLED = "fulfilled", "Выполнен"
        REJECTED = "rejected", "Отклонён"
        CANCELLED = "cancelled", "Отменён"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="staffing_requests",
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="staffing_requests")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="staffing_requests",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_staffing_requests",
    )
    requested_count = models.PositiveIntegerField()
    description = models.TextField()
    specialization = models.CharField(max_length=200, blank=True, default="")
    desired_start = models.DateField(null=True, blank=True)
    duration_days = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="handled_staffing_requests",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(requested_count__gt=0), name="staffing_requested_count_positive",
            ),
            models.CheckConstraint(
                condition=Q(duration_days__isnull=True) | Q(duration_days__gt=0),
                name="staffing_duration_days_positive",
            ),
            models.UniqueConstraint(
                fields=["organization", "project", "id"], name="unique_staffing_request_scope_id",
            ),
        ]


class StaffingAllocation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает ответа"
        ACCEPTED = "accepted", "Принято"
        DECLINED = "declined", "Отклонено"
        REVOKED = "revoked", "Отозвано"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="staffing_allocations",
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="staffing_allocations")
    request = models.ForeignKey(StaffingRequest, on_delete=models.CASCADE, related_name="allocations")
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="staffing_allocations",
    )
    department_membership = models.ForeignKey(
        "departments.DepartmentMembership", on_delete=models.PROTECT, related_name="staffing_allocations",
    )
    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_staffing_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    mode = models.CharField(max_length=10, choices=Organization.ProjectAssignmentMode)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "organization_membership"], condition=Q(status="pending"),
                name="unique_pending_project_staffing_allocation",
            ),
        ]
