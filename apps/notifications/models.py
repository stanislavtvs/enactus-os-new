from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Organization


class NotificationEvent(models.Model):
    class Category(models.TextChoices):
        TASKS = "tasks", "Задачи"
        REVIEWS = "reviews", "Согласования"
        PROJECTS = "projects", "Проекты"
        CALENDAR = "calendar", "Календарь"
        ORGANIZATION = "organization", "Организация"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="notification_events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="initiated_notification_events",
    )
    kind = models.CharField(max_length=80)
    category = models.CharField(max_length=12, choices=Category)
    object_type = models.CharField(max_length=64)
    object_id = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=255)
    data = models.JSONField(default=dict, blank=True)
    recipient_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=80, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "id"], name="unique_notification_event_organization_id",
            ),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"], name="unique_notification_event_key",
            ),
            models.CheckConstraint(condition=Q(object_id__gt=0), name="notification_object_id_positive"),
        ]
        indexes = [
            models.Index(fields=["id"], condition=Q(delivered_at__isnull=True), name="notification_pending_event_idx"),
        ]


class Notification(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="notifications")
    event = models.ForeignKey(NotificationEvent, on_delete=models.CASCADE, related_name="notifications")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "recipient"], name="unique_notification_event_recipient"),
        ]
        indexes = [
            models.Index(fields=["recipient", "-id"], name="notification_recipient_order"),
            models.Index(
                fields=["recipient", "organization"], condition=Q(read_at__isnull=True),
                name="notification_unread_recipient",
            ),
        ]
