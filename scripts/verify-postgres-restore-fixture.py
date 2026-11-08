import json
import os
import re
import sys
from datetime import timedelta
from unittest.mock import patch

import django
from django.core.serializers.json import DjangoJSONEncoder

django.setup()

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.calendar_app.invitation_models import CalendarRSVPHistory
from apps.calendar_app.invitation_services import respond_to_invitation, send_invitations
from apps.calendar_app.models import CalendarEvent
from apps.calendar_app.services import create_event
from apps.departments.services import create_department
from apps.notifications.models import Notification, NotificationEvent
from apps.notifications.services import mark_read
from apps.organizations.models import Organization, OwnershipTransfer
from apps.organizations.onboarding import issue_invitation, respond_to_invitation as join_organization
from apps.organizations.ownership import offer_ownership, respond_to_offer
from apps.organizations.services import create_organization
from apps.projects.services import create_project
from apps.tasks.services import create_task


ORGANIZATION_NAME = "Synthetic restore verification"
OWNER_USERNAME = "restore-original-owner"
RECIPIENT_USERNAME = "restore-new-owner"


def verify_database_scope(mode):
    project = os.environ["RESTORE_VERIFY_PROJECT"]
    match = re.fullmatch(r"enactus_restore_verify_([0-9a-f]{12})", project)
    if match is None:
        raise RuntimeError("The fixture requires an isolated restore verification project.")
    suffix = match.group(1)
    source = f"test_enactus_restore_source_{suffix}"
    target = f"test_enactus_restore_target_{suffix}"
    allowed = {source} if mode == "seed" else {source, target}
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        database, user = cursor.fetchone()
    if (
        database not in allowed
        or user != "enactus_restore_verify"
        or connection.settings_dict["HOST"] != "db"
    ):
        raise RuntimeError("The fixture refused to operate on a non-verification database.")


@transaction.atomic
def seed():
    users = get_user_model().objects
    owner = users.create_superuser(username=OWNER_USERNAME)
    recipient = users.create_user(username=RECIPIENT_USERNAME)
    organization = create_organization(user=owner, name=ORGANIZATION_NAME)
    invite, _ = issue_invitation(actor=owner, org_id=organization.pk, username=recipient.username)
    join_organization(actor=recipient, invitation_id=invite.pk, accept=True)
    owner_member = organization.memberships.get(user=owner)
    recipient_member = organization.memberships.get(user=recipient)

    department = create_department(user=owner, org_id=organization.pk, name="Restore department")
    project = create_project(
        user=owner, org_id=organization.pk, name="Restore project",
        lead_membership_id=owner_member.pk, responsible_department_id=department.pk,
    )
    starts_at = timezone.now().replace(microsecond=0) + timedelta(days=2)
    create_task(
        user=owner, org_id=organization.pk, name="Restore task",
        project_id=project.pk, department_id=department.pk, due_at=starts_at,
    )
    event = create_event(
        user=owner, org_id=organization.pk, title="Restore meeting", description="Synthetic data",
        event_type=CalendarEvent.Type.MEETING, starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1), timezone_name="UTC",
        project_id=project.pk, department_id=department.pk, rsvp_mode=CalendarEvent.RSVPMode.REQUIRED,
    )
    send_invitations(
        user=owner, org_id=organization.pk, event_id=event["id"], member_ids=[recipient_member.pk],
        organization=False, departments=[], project_ids=[],
    )
    invitation = organization.calendar_events.get(pk=event["id"]).invitations.get(
        recipient=recipient_member,
    )
    respond_to_invitation(
        user=recipient, invitation_id=invitation.pk, response="yes",
        expected_revision=1, expected_response_at=None,
    )
    offer = offer_ownership(user=owner, org_id=organization.pk, recipient_id=recipient_member.pk)
    respond_to_offer(user=recipient, transfer_id=offer.pk, accept=True)
    notification = Notification.objects.get(
        organization=organization, recipient=recipient, event__kind="calendar.invited",
    )
    mark_read(user=recipient, notification_id=notification.pk)

    # A real second offer leaves a durable pending delivery after a simulated
    # transient failure. Make its backoff due so the replay does not need sleep.
    with patch("apps.notifications.services._deliver_notifications", side_effect=RuntimeError):
        pending_offer = offer_ownership(user=recipient, org_id=organization.pk, recipient_id=owner_member.pk)
    pending_event = NotificationEvent.objects.get(
        organization=organization, idempotency_key=f"ownership-transfer:{pending_offer.pk}:offered",
    )
    if pending_event.delivered_at is not None or pending_event.delivery_attempts != 1:
        raise RuntimeError("The fixture did not leave the expected retryable notification.")
    NotificationEvent.objects.filter(pk=pending_event.pk).update(next_attempt_at=timezone.now())


def snapshot():
    organization = Organization.objects.select_related("owner").get(name=ORGANIZATION_NAME)
    project = organization.projects.get(name="Restore project")
    department = organization.departments.get(name="Restore department")
    task = organization.tasks.get(name="Restore task")
    event = organization.calendar_events.get(title="Restore meeting")
    offer = organization.ownership_transfers.get(status=OwnershipTransfer.Status.ACCEPTED)
    history = CalendarRSVPHistory.objects.get(organization=organization, event=event, response="yes")
    if not (
        organization.owner.username == RECIPIENT_USERNAME
        and offer.recipient.user_id == organization.owner_id
        and offer.from_owner.user.username == OWNER_USERNAME
        and offer.from_owner.is_active and offer.recipient.is_active
        and project.responsible_department_id == department.pk
        and project.lead_membership_id == offer.from_owner_id
        and task.project_id == event.project_id == project.pk
        and task.department_id == event.department_id == department.pk
        and task.due_at == event.starts_at
        and task.workflow_status.organization_id == organization.pk
        and task.priority_level.organization_id == organization.pk
        and history.invitation.recipient_id == history.actor_id == offer.recipient_id
        and history.invitation.response == history.response
    ):
        raise RuntimeError("Synthetic ownership, project, task or RSVP relationships differ.")

    audit = AuditEvent.objects.filter(organization=organization)
    if not (
        audit.filter(object_type="task", object_id=task.pk).exists()
        and audit.filter(object_type="calendar_event", object_id=event.pk).exists()
        and audit.filter(object_type="ownership_transfer", object_id=offer.pk).exists()
    ):
        raise RuntimeError("Expected audit history is missing.")
    notification_events = NotificationEvent.objects.filter(organization=organization)
    notifications = Notification.objects.filter(organization=organization)
    if not (
        notifications.filter(recipient_id=organization.owner_id, read_at__isnull=False).exists()
        and notification_events.filter(object_type="ownership_transfer", object_id=offer.pk).exists()
        and notifications.filter(event__object_type="ownership_transfer", event__object_id=offer.pk).exists()
    ):
        raise RuntimeError("Expected notification delivery or read state is missing.")

    rows = {
        "organization": Organization.objects.all(),
        "membership": organization.memberships.all(),
        "department": organization.departments.all(),
        "project": organization.projects.all(),
        "task": organization.tasks.all(),
        "calendar": organization.calendar_events.all(),
        "invitation": event.invitations.all(),
        "rsvp_history": CalendarRSVPHistory.objects.filter(organization=organization),
        "ownership_transfer": organization.ownership_transfers.all(),
        "audit": audit,
        "notification_event": notification_events,
        "notification": notifications,
        "django_migrations": MigrationRecorder.Migration.objects.all(),
    }
    data = {key: list(query.order_by("pk").values()) for key, query in rows.items()}
    print(json.dumps({"counts": {key: len(value) for key, value in data.items()}, "rows": data},
                     cls=DjangoJSONEncoder, sort_keys=True))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "snapshot"}:
        raise SystemExit("Usage: verify-postgres-restore-fixture.py seed|snapshot")
    mode = sys.argv[1]
    verify_database_scope(mode)
    if mode == "seed":
        seed()
    else:
        snapshot()


if __name__ == "__main__":
    main()
