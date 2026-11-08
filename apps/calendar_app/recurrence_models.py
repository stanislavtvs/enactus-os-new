from django.db import models
from django.db.models import F, Q

from apps.organizations.models import Organization

MAX_RECURRENCE_INTERVAL = 365
MAX_RECURRENCE_COUNT = 10_000
EVENT_MODEL = "calendar_app.CalendarEvent"


class CalendarRecurrenceRule(models.Model):
    class Frequency(models.TextChoices):
        DAILY = "daily", "Каждый день"
        WEEKLY = "weekly", "Каждую неделю"
        MONTHLY = "monthly", "Каждый месяц"
        YEARLY = "yearly", "Каждый год"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.OneToOneField(EVENT_MODEL, on_delete=models.CASCADE, related_name="recurrence_rule")
    frequency = models.CharField(max_length=7, choices=Frequency)
    interval = models.PositiveSmallIntegerField(default=1)
    weekdays = models.JSONField(default=list, blank=True)
    until = models.DateField(null=True, blank=True)
    count = models.PositiveIntegerField(null=True, blank=True)
    timezone = models.CharField(max_length=64)
    stopped_from = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(interval__gte=1, interval__lte=MAX_RECURRENCE_INTERVAL),
                name="calendar_recurrence_interval_range",
            ),
            models.CheckConstraint(
                condition=Q(count__isnull=True) | Q(count__gte=1, count__lte=MAX_RECURRENCE_COUNT),
                name="calendar_recurrence_count_range",
            ),
            models.CheckConstraint(
                condition=Q(count__isnull=True) | Q(until__isnull=True),
                name="calendar_recurrence_single_end_condition",
            ),
        ]


class CalendarOccurrenceException(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE, related_name="occurrence_exceptions")
    occurrence_key = models.DateField()
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=200, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=240, null=True, blank=True)
    online_url = models.URLField(max_length=2048, null=True, blank=True)
    is_cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    rsvp_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "occurrence_key"], name="unique_calendar_occurrence_exception"),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True, ends_at__isnull=True)
                | Q(starts_at__isnull=False, ends_at__isnull=False, ends_at__gt=F("starts_at")),
                name="calendar_occurrence_exception_interval",
            ),
            models.CheckConstraint(
                condition=Q(is_cancelled=True, cancelled_at__isnull=False)
                | Q(is_cancelled=False, cancelled_at__isnull=True),
                name="calendar_occurrence_exception_cancellation",
            ),
            models.CheckConstraint(condition=Q(version__gt=0), name="calendar_occurrence_exception_version_positive"),
        ]
        indexes = [
            models.Index(
                fields=["organization", "starts_at"],
                condition=Q(starts_at__isnull=False),
                name="calendar_occurrence_move_idx",
            ),
        ]
