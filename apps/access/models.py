from django.db import models
from django.db.models import Q

from apps.departments.models import DepartmentMembership
from apps.organizations.models import Organization, OrganizationMembership

ROLE_NAME_MAX_LENGTH = 120
POSITION_TITLE_MAX_LENGTH = 120


class Role(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    name = models.CharField(max_length=ROLE_NAME_MAX_LENGTH)
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="unique_role_organization_id"),
        ]


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    code = models.CharField(max_length=64)
    include_descendants = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "code"], name="unique_role_permission_code"),
        ]


class RoleAssignment(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="role_assignments",
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="assignments")
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="role_assignments",
    )
    department_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.CASCADE, null=True, blank=True,
        related_name="role_assignments",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "organization_membership", "department_membership"],
                condition=Q(revoked_at__isnull=True), nulls_distinct=False,
                name="unique_active_role_assignment",
            ),
        ]


class PositionAssignment(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="position_assignments",
    )
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="position_assignments",
    )
    department_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.CASCADE, null=True, blank=True,
        related_name="position_assignments",
    )
    title = models.CharField(max_length=POSITION_TITLE_MAX_LENGTH)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization_membership", "department_membership", "title"],
                condition=Q(revoked_at__isnull=True), nulls_distinct=False,
                name="unique_active_position_assignment",
            ),
        ]
