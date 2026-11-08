from django.db import models
from django.db.models import F, Q

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.projects.models import Project

EVENT_TITLE_MAX_LENGTH = 200
EVENT_LOCATION_MAX_LENGTH = 240


class CalendarEvent(models.Model):
    class Type(models.TextChoices):
        MEETING = "meeting", "Встреча"
        EVENT = "event", "Мероприятие"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Запланировано"
        CANCELLED = "cancelled", "Отменено"
        ARCHIVED = "archived", "Архив"

    class RSVPMode(models.TextChoices):
        INFORMATIONAL = "informational", "Информационное"
        OPTIONAL = "optional", "Необязательный ответ"
        REQUIRED = "required", "Обязательный ответ"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="calendar_events")
    created_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="created_calendar_events",
    )
    title = models.CharField(max_length=EVENT_TITLE_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    event_type = models.CharField(max_length=7, choices=Type)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    timezone = models.CharField(max_length=64)
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=EVENT_LOCATION_MAX_LENGTH, blank=True, default="")
    online_url = models.URLField(max_length=2048, blank=True, default="")
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="calendar_events",
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="calendar_events",
    )
    status = models.CharField(max_length=9, choices=Status, default=Status.SCHEDULED)
    rsvp_mode = models.CharField(max_length=13, choices=RSVPMode, default=RSVPMode.INFORMATIONAL)
    rsvp_revision = models.PositiveIntegerField(default=1)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="unique_calendar_event_organization_id"),
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")), name="calendar_event_positive_interval"),
            models.CheckConstraint(condition=Q(rsvp_revision__gt=0), name="calendar_event_rsvp_revision_positive"),
            models.CheckConstraint(
                condition=(
                    Q(status="scheduled", cancelled_at__isnull=True, archived_at__isnull=True)
                    | Q(status="cancelled", cancelled_at__isnull=False, archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="calendar_event_lifecycle_timestamps",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "starts_at", "id"], name="calendar_event_org_start_idx"),
            models.Index(fields=["organization", "ends_at"], name="calendar_event_org_end_idx"),
        ]


from .invitation_models import (  # noqa: E402, F401
    CalendarAudience,
    CalendarInvitation,
    CalendarInvitationSource,
    CalendarRSVPHistory,
)
from .recurrence_models import CalendarOccurrenceException, CalendarRecurrenceRule  # noqa: E402, F401
from .recurrence_rsvp_models import CalendarOccurrenceRSVP, CalendarOccurrenceRSVPHistory  # noqa: E402, F401
