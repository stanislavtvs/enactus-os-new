from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.departments.models import Department
from apps.organizations.models import Organization
from apps.projects.models import Project


class AuditEvent(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="initiated_audit_events",
    )
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=64)
    object_id = models.PositiveBigIntegerField()
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events",
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events",
    )
    changes = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    operation_id = models.UUIDField(default=uuid4)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_audit_event_organization_key",
            ),
            models.CheckConstraint(condition=Q(object_id__gt=0), name="audit_event_object_id_positive"),
        ]
        indexes = [
            models.Index(
                fields=["organization", "-created_at", "-id"], name="audit_org_timeline_idx",
            ),
            models.Index(
                fields=["organization", "object_type", "object_id", "-created_at", "-id"],
                name="audit_object_timeline_idx",
            ),
            models.Index(
                fields=["organization", "department", "-created_at", "-id"],
                condition=Q(department__isnull=False),
                name="audit_department_timeline_idx",
            ),
            models.Index(
                fields=["organization", "project", "-created_at", "-id"],
                condition=Q(project__isnull=False),
                name="audit_project_timeline_idx",
            ),
            models.Index(
                fields=["organization", "actor", "-created_at", "-id"],
                condition=Q(actor__isnull=False),
                name="audit_actor_timeline_idx",
            ),
        ]
