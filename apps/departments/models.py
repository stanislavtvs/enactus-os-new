from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.organizations.models import Organization, OrganizationMembership

DEPARTMENT_NAME_MAX_LENGTH = 200


class Department(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="departments",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    name = models.CharField(max_length=DEPARTMENT_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(parent_id=F("id")),
                name="department_parent_is_not_self",
            ),
            models.UniqueConstraint(
                fields=["organization", "id"],
                name="unique_department_organization_id",
            ),
        ]

    def __str__(self):
        return self.name


class DepartmentMembership(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="department_memberships",
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name="memberships",
    )
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="department_memberships",
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["department", "organization_membership"],
                name="unique_department_membership",
            ),
        ]


class DepartmentInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает ответа"
        ACCEPTED = "accepted", "Принято"
        DECLINED = "declined", "Отклонено"
        REVOKED = "revoked", "Отозвано"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="department_invitations",
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name="invitations",
    )
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="department_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="sent_department_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["department", "organization_membership"],
                condition=Q(status="pending"),
                name="unique_pending_department_invitation",
            ),
        ]
