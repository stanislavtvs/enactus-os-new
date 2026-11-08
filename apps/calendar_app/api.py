from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import HttpResponse
from ninja import PatchDict, Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access
from apps.organizations.modules import CALENDAR

from . import services
from .schemas import (
    CalendarFilters,
    CalendarOutput,
    CalendarQuery,
    EventInput,
    EventListOutput,
    EventOutput,
    EventQuery,
    RecordId,
)

router = Router(auth=django_auth)
EVENT_CONFLICT_CONSTRAINTS = {
    "calendar_event_lifecycle_timestamps",
    "calendar_event_project_not_archived",
    "calendar_event_department_not_archived",
}


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except services.CalendarConflict as exc:
        raise HttpError(409, str(exc)) from None
    except services.CalendarValidation as exc:
        raise HttpError(422, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in EVENT_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Состояние события или связанного объекта изменилось.") from None


def _filters(payload):
    filters = payload.model_dump()
    filters["timezone_name"] = filters.pop("timezone")
    return filters


def _event_changes(payload):
    if "type" in payload:
        payload["event_type"] = payload.pop("type")
    if "location" in payload:
        payload["location"] = payload["location"] or ""
    if "online_url" in payload:
        payload["online_url"] = str(payload["online_url"] or "")
    return payload


@router.get("/{org_id}/calendar/", response=CalendarOutput, url_name="calendar_feed")
def get_calendar(request, response: HttpResponse, org_id: RecordId, filters: Query[CalendarQuery]):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    response["Cache-Control"] = "no-store"
    return {"items": _perform(services.calendar_feed, access=access, **_filters(filters))}


@router.get("/{org_id}/calendar/filters/", response=CalendarFilters, url_name="calendar_filters")
def get_calendar_filters(request, org_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    return services.calendar_filters(access=access)


@router.get("/{org_id}/events/", response=EventListOutput, url_name="calendar_event_list")
def list_calendar_events(request, response: HttpResponse, org_id: RecordId, filters: Query[EventQuery]):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    response["Cache-Control"] = "no-store"
    return {"items": _perform(services.list_events, access=access, **_filters(filters))}


@router.post("/{org_id}/events/", response={201: EventOutput})
def create_calendar_event(request, org_id: RecordId, payload: EventInput):
    fields = _event_changes(payload.model_dump())
    fields["timezone_name"] = fields.pop("timezone")
    return 201, _perform(services.create_event, user=request.user, org_id=org_id, **fields)


@router.get("/{org_id}/events/{event_id}/", response=EventOutput, url_name="calendar_event_detail")
def get_calendar_event(request, response: HttpResponse, org_id: RecordId, event_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    response["Cache-Control"] = "no-store"
    event = services.visible_event(access=access, event_id=event_id, allow_invitation=True)
    return services.event_item(access, event)


@router.patch("/{org_id}/events/{event_id}/", response=EventOutput)
def update_calendar_event(request, org_id: RecordId, event_id: RecordId, payload: PatchDict[EventInput]):
    return _perform(
        services.update_event, user=request.user, org_id=org_id, event_id=event_id,
        changes=_event_changes(payload),
    )


@router.post("/{org_id}/events/{event_id}/cancel/", response=EventOutput)
def cancel_calendar_event(request, org_id: RecordId, event_id: RecordId):
    return _perform(services.cancel_event, user=request.user, org_id=org_id, event_id=event_id)


@router.post("/{org_id}/events/{event_id}/archive/", response=EventOutput)
def archive_calendar_event(request, org_id: RecordId, event_id: RecordId):
    return _perform(services.archive_event, user=request.user, org_id=org_id, event_id=event_id)
