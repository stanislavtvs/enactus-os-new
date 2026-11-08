from django.conf import settings
from django.db import models
from django.db.models import Q

ORGANIZATION_NAME_MAX_LENGTH = 200


class Organization(models.Model):
    class ProjectAssignmentMode(models.TextChoices):
        DIRECT = "direct", "Прямое назначение"
        INVITATION = "invitation", "Приглашение с подтверждением"

    class DepartmentDirectoryVisibility(models.TextChoices):
        BRANCH = "branch", "Своё подразделение и его ветка"
        ORGANIZATION = "organization", "Вся организация"

    class JoinPolicy(models.TextChoices):
        INVITES = "invites", "Только приглашения"
        REQUESTS = "requests", "Только заявки"
        BOTH = "both", "Приглашения и заявки"

    name = models.CharField(max_length=ORGANIZATION_NAME_MAX_LENGTH)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    project_assignment_mode = models.CharField(
        max_length=10,
        choices=ProjectAssignmentMode,
        default=ProjectAssignmentMode.DIRECT,
    )
    department_directory_visibility = models.CharField(
        max_length=12,
        choices=DepartmentDirectoryVisibility,
        default=DepartmentDirectoryVisibility.BRANCH,
    )
    join_policy = models.CharField(
        max_length=8,
        choices=JoinPolicy,
        default=JoinPolicy.INVITES,
    )

    def __str__(self):
        return self.name


class OrganizationMembership(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    is_active = models.BooleanField(default=True)
    can_manage_members = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_organization_membership",
            ),
            models.UniqueConstraint(
                fields=["organization", "id"],
                name="unique_org_membership_organization_id",
            ),
        ]


class OrganizationInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает ответа"
        ACCEPTED = "accepted", "Принято"
        DECLINED = "declined", "Отклонено"
        REVOKED = "revoked", "Отозвано"
        EXPIRED = "expired", "Истекло"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_organization_invitations",
    )
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "recipient"],
                condition=Q(status="pending"),
                name="unique_pending_organization_invitation",
            ),
        ]


class JoinRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "На рассмотрении"
        APPROVED = "approved", "Одобрена"
        REJECTED = "rejected", "Отклонена"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_join_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_organization_join_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "applicant"],
                condition=Q(status="pending"),
                name="unique_pending_organization_join_request",
            ),
        ]
