from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError
from django.http import HttpResponse
from ninja import Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from . import invitation_services as services
from . import occurrence_rsvp
from .invitation_schemas import (
    AudienceInput,
    AudiencePreview,
    AudienceSent,
    ManagedInvitations,
    OccurrenceRSVPHistoryOutput,
    OccurrenceRSVPInput,
    OwnInvitation,
    OwnInvitations,
    OwnOccurrenceInvitation,
    RecipientOptions,
    RecipientOptionsQuery,
    RSVPStats,
    RSVPAnswerInput,
    RSVPHistoryOutput,
)
from .schemas import RecordId
from .services import CalendarConflict, CalendarValidation

router = Router(auth=django_auth)
INVITATION_CONFLICT_CONSTRAINTS = {
    "calendar_invitation_active_parents",
    "calendar_invitation_response_revision_current",
    "unique_active_calendar_invitation",
}


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except CalendarConflict as exc:
        raise HttpError(409, str(exc)) from None
    except CalendarValidation as exc:
        raise HttpError(422, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in INVITATION_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Состояние события или получателя изменилось.") from None
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != "55P03":
            raise
        raise HttpError(409, "Состояние приглашения меняется. Обновите страницу и повторите действие.") from None


@router.get("/{org_id}/events/{event_id}/invitations/options/", response=RecipientOptions)
def recipient_options(request, org_id: RecordId, event_id: RecordId, filters: Query[RecipientOptionsQuery]):
    return _perform(
        services.audience_options, user=request.user, org_id=org_id, event_id=event_id, **filters.model_dump(),
    )


@router.post("/{org_id}/events/{event_id}/invitations/preview/", response=AudiencePreview)
def preview_audience(request, org_id: RecordId, event_id: RecordId, payload: AudienceInput):
    return _perform(
        services.preview_audience, user=request.user, org_id=org_id, event_id=event_id, **payload.model_dump(),
    )


@router.post("/{org_id}/events/{event_id}/invitations/", response={201: AudienceSent})
def send_invitations(request, org_id: RecordId, event_id: RecordId, payload: AudienceInput):
    return 201, _perform(
        services.send_invitations, user=request.user, org_id=org_id, event_id=event_id, **payload.model_dump(),
    )


@router.post("/{org_id}/events/{event_id}/invitations/refresh/", response=AudienceSent)
def refresh_audience(request, org_id: RecordId, event_id: RecordId):
    return _perform(services.refresh_audience, user=request.user, org_id=org_id, event_id=event_id)


@router.get("/{org_id}/events/{event_id}/invitations/", response=ManagedInvitations)
def list_event_invitations(request, org_id: RecordId, event_id: RecordId):
    event, policy, invitations = _perform(
        services.list_event_invitations, user=request.user, org_id=org_id, event_id=event_id,
    )
    return {"items": [
        {
            "id": invitation.pk,
            "username": invitation.recipient.user.get_username(),
            "recipient_id": invitation.recipient_id,
            "response": services.effective_response(invitation, event),
            "responded_at": invitation.responded_at,
            "revoked_at": invitation.revoked_at,
            "needs_reconfirmation": services.needs_reconfirmation(invitation, event),
            "can_revoke": services.can_revoke_invitation(policy, invitation),
            "created_at": invitation.created_at,
            "sent_at": invitation.sent_at,
        }
        for invitation in invitations
    ]}


@router.get("/{org_id}/events/{event_id}/invitations/stats/", response=RSVPStats)
def invitation_stats(request, org_id: RecordId, event_id: RecordId):
    return _perform(services.invitation_stats, user=request.user, org_id=org_id, event_id=event_id)


@router.post("/{org_id}/events/{event_id}/invitations/{invitation_id}/revoke/", response={204: None})
def revoke_invitation(request, org_id: RecordId, event_id: RecordId, invitation_id: RecordId):
    _perform(
        services.revoke_invitation, user=request.user, org_id=org_id, event_id=event_id, invitation_id=invitation_id,
    )
    return 204, None


@router.get("/{org_id}/events/{event_id}/invitations/{invitation_id}/history/", response=RSVPHistoryOutput)
def invitation_history(request, response: HttpResponse, org_id: RecordId, event_id: RecordId, invitation_id: RecordId):
    history = _perform(
        services.invitation_history, user=request.user, org_id=org_id, event_id=event_id, invitation_id=invitation_id,
    )
    response["Cache-Control"] = "no-store"
    return {"items": [
        {
            "id": entry.pk,
            "response": entry.response,
            "response_revision": entry.response_revision,
            "actor": entry.actor.user.get_username(),
            "created_at": entry.created_at,
        }
        for entry in history
    ]}


@router.get("/calendar/invitations/", response=OwnInvitations, url_name="calendar_invitation_inbox")
def own_invitations(request, response: HttpResponse, org_id: RecordId | None = None):
    invitations = services.own_invitations(user=request.user, org_id=org_id)
    response["Cache-Control"] = "no-store"
    return {"items": [services.own_invitation_data(invitation) for invitation in invitations]}


@router.get("/calendar/invitations/{invitation_id}/", response=OwnInvitation, url_name="calendar_invitation_detail")
def own_invitation(request, response: HttpResponse, invitation_id: RecordId):
    invitation = services.own_invitation_by_id(user=request.user, invitation_id=invitation_id)
    response["Cache-Control"] = "no-store"
    return services.own_invitation_data(invitation)


@router.post("/calendar/invitations/{invitation_id}/respond/", response=OwnInvitation)
def respond_to_invitation(request, response: HttpResponse, invitation_id: RecordId, payload: RSVPAnswerInput):
    invitation = _perform(
        services.respond_to_invitation, user=request.user, invitation_id=invitation_id, **payload.model_dump(),
    )
    response["Cache-Control"] = "no-store"
    return services.own_invitation_data(invitation)


@router.get("/calendar/invitations/{invitation_id}/occurrences/{key}/", response=OwnOccurrenceInvitation)
def own_occurrence(request, response: HttpResponse, invitation_id: RecordId, key: date):
    response["Cache-Control"] = "no-store"
    return _perform(occurrence_rsvp.own_occurrence, user=request.user, invitation_id=invitation_id, key=key)


@router.post("/calendar/invitations/{invitation_id}/occurrences/{key}/respond/", response=OwnOccurrenceInvitation)
def respond_to_occurrence(
    request, response: HttpResponse, invitation_id: RecordId, key: date, payload: OccurrenceRSVPInput,
):
    response["Cache-Control"] = "no-store"
    return _perform(
        occurrence_rsvp.respond, user=request.user, invitation_id=invitation_id, key=key, **payload.model_dump(),
    )


@router.get(
    "/{org_id}/events/{event_id}/occurrences/{key}/invitations/{invitation_id}/history/",
    response=OccurrenceRSVPHistoryOutput,
)
def occurrence_history(
    request, response: HttpResponse, org_id: RecordId, event_id: RecordId, invitation_id: RecordId, key: date,
):
    response["Cache-Control"] = "no-store"
    return _perform(
        occurrence_rsvp.history, user=request.user, org_id=org_id, event_id=event_id,
        invitation_id=invitation_id, key=key,
    )
