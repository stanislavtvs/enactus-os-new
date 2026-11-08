from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access

from .audit import calendar_action
from . import invitation_services as invitations
from .models import CalendarEvent
from .recurrence import resolve_occurrence
from .recurrence_rsvp_models import CalendarOccurrenceRSVP, CalendarOccurrenceRSVPHistory
from .services import CalendarConflict


def _responses(invitation, key):
    return CalendarOccurrenceRSVP.objects.filter(
        organization_id=invitation.organization_id, event_id=invitation.event_id,
        invitation=invitation, occurrence_key=key,
    )


def _current_response(record, event, occurrence):
    return record is not None and (
        record.response_revision == event.rsvp_revision
        and record.occurrence_rsvp_revision == occurrence.rsvp_revision
    )


def _can_respond(invitation, occurrence):
    return (
        invitations._can_respond(invitation, invitation.event)
        and occurrence.status == CalendarEvent.Status.SCHEDULED
    )


def _data(invitation, occurrence, record):
    data = invitations.own_invitation_data(invitation)
    event = invitation.event
    series_response = data["response"]
    enabled = event.rsvp_mode != CalendarEvent.RSVPMode.INFORMATIONAL
    response = None
    if enabled:
        if _current_response(record, event, occurrence):
            response = record.response
        elif record is None and occurrence.rsvp_revision == 0:
            response = series_response
    reconfirm = enabled and response is None and (
        record is not None
        or data["needs_reconfirmation"]
        or (occurrence.rsvp_revision > 0 and invitation.response is not None)
    )
    data.update({
        "occurrence_key": occurrence.key,
        "occurrence_rsvp_revision": occurrence.rsvp_revision,
        "series_response": series_response,
        "response": response,
        "response_revision": record.response_revision if record is not None else (
            invitation.response_revision if response is not None else None
        ),
        "responded_at": record.responded_at.isoformat() if record is not None else None,
        "needs_reconfirmation": reconfirm,
        "can_respond": _can_respond(invitation, occurrence),
    })
    details = data["event"]
    details.update({
        "starts_at": occurrence.starts_at,
        "ends_at": occurrence.ends_at,
        "status": occurrence.status,
        "status_label": CalendarEvent.Status(occurrence.status).label,
    })
    if occurrence.exception is not None:
        for field in ("title", "location", "online_url"):
            value = getattr(occurrence.exception, field)
            if value is not None:
                details[field] = value if field == "title" else value or None
    return data


def own_occurrence(*, user, invitation_id, key):
    invitation = invitations.own_invitation_by_id(user=user, invitation_id=invitation_id)
    occurrence = resolve_occurrence(invitation.event, key)
    return _data(invitation, occurrence, _responses(invitation, key).first())


@transaction.atomic
def respond(
    *, user, invitation_id, key, response, expected_revision,
    expected_occurrence_revision, expected_response_at,
):
    locator = get_object_or_404(
        invitations._own_rows(user).values("organization_id", "event_id"), pk=invitation_id,
    )
    access = organization_access(user=user, org_id=locator["organization_id"], lock=True)
    event = get_object_or_404(
        access.organization.calendar_events.select_for_update(of=("self",)).select_related(
            "organization", "created_by__user", "recurrence_rule",
        ),
        pk=locator["event_id"],
    )
    invitation = get_object_or_404(
        invitations._recipient_invitations(access).select_for_update(of=("self",)),
        event=event, pk=invitation_id,
    )
    invitation.event = event
    occurrence = resolve_occurrence(event, key)
    if not _can_respond(invitation, occurrence):
        raise CalendarConflict("Встреча больше не принимает ответы.")
    if expected_revision != event.rsvp_revision or expected_occurrence_revision != occurrence.rsvp_revision:
        raise CalendarConflict("Расписание изменилось. Ознакомьтесь с новым временем и ответьте ещё раз.")

    rows = _responses(invitation, key)
    record = rows.first()
    if _current_response(record, event, occurrence) and record.response == response:
        return _data(invitation, occurrence, record)
    if (record.responded_at if record is not None else None) != expected_response_at:
        raise CalendarConflict("Ответ уже изменился. Обновите приглашение и повторите действие.")

    fields = {
        "response": response,
        "response_revision": event.rsvp_revision,
        "occurrence_rsvp_revision": occurrence.rsvp_revision,
        "responded_at": timezone.now(),
    }
    if record is None:
        record = CalendarOccurrenceRSVP.objects.create(
            organization=event.organization, event=event, invitation=invitation, occurrence_key=key, **fields,
        )
    else:
        for field, value in fields.items():
            setattr(record, field, value)
        record.save(update_fields=list(fields))
    history_entry = CalendarOccurrenceRSVPHistory.objects.create(
        organization=event.organization, event=event, invitation=invitation, occurrence_key=key,
        actor=access.membership, response=response, response_revision=event.rsvp_revision,
        occurrence_rsvp_revision=occurrence.rsvp_revision,
    )
    calendar_action(
        user, event, "rsvp_occurrence_recorded", metadata={"history_id": history_entry.pk, "occurrence_key": key},
    )
    return _data(invitation, occurrence, record)


def history(*, user, org_id, event_id, invitation_id, key):
    access, event = invitations._organizer(user, org_id, event_id, manage=True)
    invitation = get_object_or_404(event.invitations, organization=access.organization, pk=invitation_id)
    rows = invitation.occurrence_response_history.filter(
        organization=access.organization, event=event, occurrence_key=key,
    ).select_related("actor__user").order_by("pk")
    if not rows.exists():
        resolve_occurrence(event, key)
    return {"items": [
        {
            "id": entry.pk, "response": entry.response, "response_revision": entry.response_revision,
            "occurrence_rsvp_revision": entry.occurrence_rsvp_revision,
            "actor": entry.actor.user.get_username(), "created_at": entry.created_at,
        }
        for entry in rows
    ]}
