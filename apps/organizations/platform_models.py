from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .models import ORGANIZATION_NAME_MAX_LENGTH


USER_MODEL = settings.AUTH_USER_MODEL
ORGANIZATION_MODEL = "organizations.Organization"
HEX_DIGEST = r"^[0-9a-f]{64}$"


class PlatformCreationPolicy(models.Model):
    class Mode(models.TextChoices):
        OPEN = "open", "Свободное создание"
        PERMISSION_ONLY = "permission_only", "Только по разрешению"
        REQUEST_APPROVAL = "request_approval", "По заявке"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    mode = models.CharField(max_length=16, choices=Mode, default=Mode.PERMISSION_ONLY)
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="platform_creation_policy_changes",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="platform_creation_policy_singleton"),
            models.CheckConstraint(
                condition=Q(mode__in=["open", "permission_only", "request_approval"]),
                name="platform_creation_policy_mode_valid",
            ),
        ]


class PlatformCreationGrant(models.Model):
    user = models.OneToOneField(
        USER_MODEL, on_delete=models.PROTECT, related_name="platform_creation_grant",
    )
    granted_by = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, related_name="issued_platform_creation_grants",
    )
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_by = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="revoked_platform_creation_grants",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(user=F("granted_by")),
                name="platform_creation_grant_distinct_actor",
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True, revoked_by__isnull=True)
                | Q(revoked_at__isnull=False, revoked_by__isnull=False),
                name="platform_creation_grant_revocation_pair",
            ),
        ]


class OrganizationCreationRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает рассмотрения"
        APPROVED = "approved", "Одобрена"
        REJECTED = "rejected", "Отклонена"
        WITHDRAWN = "withdrawn", "Отозвана"

    applicant = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, related_name="organization_creation_requests",
    )
    name = models.CharField(max_length=ORGANIZATION_NAME_MAX_LENGTH)
    description = models.CharField(max_length=2000, blank=True, default="")
    status = models.CharField(max_length=9, choices=Status, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewer = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reviewed_organization_creation_requests",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=1000, blank=True, default="")
    organization = models.OneToOneField(
        ORGANIZATION_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="approved_creation_request",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["applicant"], condition=Q(status="pending"),
                name="unique_pending_organization_creation_request",
            ),
            models.CheckConstraint(
                condition=~Q(applicant=F("reviewer")),
                name="organization_creation_request_distinct_reviewer",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="pending", reviewer__isnull=True, decided_at__isnull=True,
                      organization__isnull=True, decision_comment="")
                    | Q(status="approved", reviewer__isnull=False, decided_at__isnull=False,
                        organization__isnull=False)
                    | Q(status="rejected", reviewer__isnull=False, decided_at__isnull=False,
                        organization__isnull=True, decision_comment__regex=r"[^[:space:]]")
                    | Q(status="withdrawn", reviewer__isnull=True, decided_at__isnull=False,
                        organization__isnull=True, decision_comment="")
                ),
                name="organization_creation_request_status_fields",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-id"], name="org_creation_request_state_idx"),
        ]


class OrganizationCreationReceipt(models.Model):
    user = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, related_name="organization_creation_receipts",
    )
    key = models.CharField(max_length=64)
    name_digest = models.CharField(max_length=64)
    organization = models.OneToOneField(
        ORGANIZATION_MODEL, on_delete=models.PROTECT, related_name="creation_receipt",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_organization_creation_receipt_key"),
            models.CheckConstraint(
                condition=Q(key__regex=HEX_DIGEST, name_digest__regex=HEX_DIGEST),
                name="organization_creation_receipt_digests_valid",
            ),
        ]


class PlatformAuditEvent(models.Model):
    actor = models.ForeignKey(
        USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="platform_audit_events",
    )
    action = models.CharField(max_length=80)
    subject_id = models.PositiveBigIntegerField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(subject_id__gt=0), name="platform_audit_subject_positive"),
        ]


class PlatformNotice(models.Model):
    class Kind(models.TextChoices):
        CREATION_SUBMITTED = "platform.creation_submitted", "Новая заявка"
        CREATION_APPROVED = "platform.creation_approved", "Заявка одобрена"
        CREATION_REJECTED = "platform.creation_rejected", "Заявка отклонена"

    recipient = models.ForeignKey(
        USER_MODEL, on_delete=models.CASCADE, related_name="platform_notices",
    )
    kind = models.CharField(max_length=40, choices=Kind)
    creation_request = models.ForeignKey(
        OrganizationCreationRequest, on_delete=models.PROTECT, related_name="notices",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "kind", "creation_request"], name="unique_platform_creation_notice",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "-id"], name="platform_notice_recipient_idx"),
        ]
