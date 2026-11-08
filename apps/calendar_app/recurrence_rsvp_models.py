from django.db import models
from django.db.models import Q

from apps.organizations.models import Organization, OrganizationMembership

from .invitation_models import CalendarInvitation
from .recurrence_models import EVENT_MODEL


class CalendarOccurrenceRSVP(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE, related_name="occurrence_responses")
    invitation = models.ForeignKey(CalendarInvitation, on_delete=models.CASCADE, related_name="occurrence_responses")
    occurrence_key = models.DateField()
    response = models.CharField(max_length=5, choices=CalendarInvitation.Response)
    response_revision = models.PositiveIntegerField()
    occurrence_rsvp_revision = models.PositiveIntegerField()
    responded_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invitation", "occurrence_key"], name="unique_calendar_occurrence_invitation_response",
            ),
            models.CheckConstraint(
                condition=Q(response__in=CalendarInvitation.Response.values, response_revision__gt=0),
                name="calendar_occurrence_response_valid",
            ),
        ]


class CalendarOccurrenceRSVPHistory(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE, related_name="occurrence_response_history")
    invitation = models.ForeignKey(CalendarInvitation, on_delete=models.CASCADE, related_name="occurrence_response_history")
    occurrence_key = models.DateField()
    response = models.CharField(max_length=5, choices=CalendarInvitation.Response)
    response_revision = models.PositiveIntegerField()
    occurrence_rsvp_revision = models.PositiveIntegerField()
    actor = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(response__in=CalendarInvitation.Response.values, response_revision__gt=0),
                name="calendar_occurrence_history_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["invitation", "occurrence_key", "id"], name="calendar_occ_rsvp_history_idx"),
        ]
