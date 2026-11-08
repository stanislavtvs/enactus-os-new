from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.projects.models import Project

EVENT_MODEL = "calendar_app.CalendarEvent"


class CalendarAudience(models.Model):
    class Kind(models.TextChoices):
        ORGANIZATION = "organization", "Вся организация"
        MEMBER = "member", "Участник"
        DEPARTMENT = "department", "Подразделение"
        PROJECT = "project", "Команда проекта"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE, related_name="audiences")
    kind = models.CharField(max_length=12, choices=Kind)
    member = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="calendar_audience_selections",
    )
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True)
    include_descendants = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="created_calendar_audiences",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "event", "id"], name="unique_calendar_audience_scope_id"),
            models.UniqueConstraint(
                fields=["event", "kind", "member", "department", "project", "include_descendants"],
                nulls_distinct=False, name="unique_calendar_audience_selection",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="organization", member__isnull=True, department__isnull=True, project__isnull=True, include_descendants=False)
                    | Q(kind="member", member__isnull=False, department__isnull=True, project__isnull=True, include_descendants=False)
                    | Q(kind="department", member__isnull=True, department__isnull=False, project__isnull=True)
                    | Q(kind="project", member__isnull=True, department__isnull=True, project__isnull=False, include_descendants=False)
                ),
                name="calendar_audience_target_matches_kind",
            ),
        ]


class CalendarInvitation(models.Model):
    class Response(models.TextChoices):
        YES = "yes", "Приду"
        NO = "no", "Не приду"
        MAYBE = "maybe", "Пока не знаю"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE, related_name="invitations")
    recipient = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="calendar_invitations",
    )
    invited_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="sent_calendar_invitations",
    )
    response = models.CharField(max_length=5, choices=Response, null=True, blank=True)
    response_revision = models.PositiveIntegerField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="revoked_calendar_invitations",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "event", "id"], name="unique_calendar_invitation_scope_id"),
            models.UniqueConstraint(
                fields=["event", "recipient"], condition=Q(revoked_at__isnull=True),
                name="unique_active_calendar_invitation",
            ),
            models.CheckConstraint(
                condition=(
                    Q(response__isnull=True, response_revision__isnull=True, responded_at__isnull=True)
                    | Q(response__in=["yes", "no", "maybe"], response__isnull=False, response_revision__gt=0,
                        response_revision__isnull=False, responded_at__isnull=False)
                ),
                name="calendar_invitation_response_fields_match",
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=False) | Q(revoked_by__isnull=True),
                name="calendar_invitation_revocation_fields_match",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recipient", "event"], condition=Q(revoked_at__isnull=True),
                name="calendar_active_recipient_idx",
            ),
        ]


class CalendarInvitationSource(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE)
    invitation = models.ForeignKey(CalendarInvitation, on_delete=models.CASCADE, related_name="sources")
    audience = models.ForeignKey(CalendarAudience, on_delete=models.PROTECT, related_name="snapshot_members")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["invitation", "audience"], name="unique_calendar_invitation_source"),
        ]


class CalendarRSVPHistory(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    event = models.ForeignKey(EVENT_MODEL, on_delete=models.CASCADE)
    invitation = models.ForeignKey(CalendarInvitation, on_delete=models.CASCADE, related_name="response_history")
    actor = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT)
    response = models.CharField(max_length=5, choices=CalendarInvitation.Response)
    response_revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(response_revision__gt=0), name="calendar_rsvp_history_revision_positive"),
        ]
