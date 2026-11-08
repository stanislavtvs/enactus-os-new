from datetime import time

from django.conf import settings
from django.db import models
from django.db.models import F, Q

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
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
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
            models.CheckConstraint(
                condition=Q(delivered_at__isnull=True) | Q(failed_at__isnull=True),
                name="notification_delivery_terminal_states_exclusive",
            ),
            models.CheckConstraint(
                condition=Q(next_attempt_at__isnull=True)
                | Q(delivered_at__isnull=True, failed_at__isnull=True),
                name="notification_retry_requires_pending_event",
            ),
        ]
        indexes = [
            models.Index(
                fields=["next_attempt_at", "id"],
                condition=Q(delivered_at__isnull=True, failed_at__isnull=True),
                name="notification_retry_pending_idx",
            ),
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


def default_notification_categories():
    return list(NotificationEvent.Category.values)


class OrganizationNotificationPolicy(models.Model):
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="notification_policy",
    )
    enabled_categories = models.JSONField(default=default_notification_categories, blank=True)
    disabled_kinds = models.JSONField(default=list, blank=True)
    task_offsets_minutes = models.JSONField(default=list, blank=True)
    calendar_offsets_minutes = models.JSONField(default=list, blank=True)
    task_overdue = models.BooleanField(default=False)
    include_collaborators = models.BooleanField(default=False)
    calendar_informational = models.BooleanField(default=True)
    all_day_reminder_time = models.TimeField(default=time(9))
    updated_at = models.DateTimeField(auto_now=True)


class NotificationScanCursor(models.Model):
    stream = models.CharField(max_length=32, primary_key=True)
    last_id = models.PositiveBigIntegerField(default=0)


class UserNotificationPreference(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="notification_preferences",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences",
    )
    enabled_categories = models.JSONField(default=default_notification_categories, blank=True)
    task_reminders = models.BooleanField(default=True)
    calendar_reminders = models.BooleanField(default=True)
    timezone = models.CharField(max_length=64, default="UTC")
    quiet_start = models.TimeField(null=True, blank=True)
    quiet_end = models.TimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="unique_user_notification_preference",
            ),
            models.CheckConstraint(
                condition=Q(quiet_start__isnull=True, quiet_end__isnull=True)
                | (
                    Q(quiet_start__isnull=False, quiet_end__isnull=False)
                    & ~Q(quiet_start=F("quiet_end"))
                ),
                name="notification_preference_quiet_interval",
            ),
        ]
