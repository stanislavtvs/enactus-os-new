from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access
from apps.organizations.modules import CALENDAR

from . import recurrence
from .recurrence_schemas import (
    ExceptionPageQuery, OccurrenceChange, OccurrenceQuery, OccurrenceVersion,
    RecurrenceChange, RecurrenceDetail, StopRecurrenceInput,
)
from .schemas import EventOutput, RecordId
from .services import CalendarConflict, CalendarValidation, visible_event

router = Router(auth=django_auth)
RECURRENCE_CONFLICT_CONSTRAINTS = {
    "calendar_recurrence_interval_range",
    "calendar_recurrence_count_range",
    "calendar_recurrence_single_end_condition",
    "unique_calendar_occurrence_exception",
    "calendar_occurrence_exception_interval",
    "calendar_occurrence_exception_cancellation",
    "calendar_occurrence_exception_version_positive",
}


class OccurrenceList(Schema):
    items: list[EventOutput]


class OccurrenceExceptions(OccurrenceList):
    page: int
    page_size: int
    total: int


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
        if constraint not in RECURRENCE_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Правило повторения изменилось.") from None


@router.get("/{org_id}/events/{event_id}/recurrence/", response=RecurrenceDetail)
def get_recurrence(request, org_id: RecordId, event_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    return recurrence.recurrence_detail(access=access, event_id=event_id)


@router.put("/{org_id}/events/{event_id}/recurrence/", response=RecurrenceDetail)
def set_recurrence(request, org_id: RecordId, event_id: RecordId, payload: RecurrenceChange):
    return _perform(
        recurrence.set_recurrence, user=request.user, org_id=org_id, event_id=event_id,
        payload=payload.recurrence.model_dump() if payload.recurrence is not None else None,
    )


@router.get("/{org_id}/events/{event_id}/occurrences/", response=OccurrenceList)
def list_occurrences(request, org_id: RecordId, event_id: RecordId, filters: Query[OccurrenceQuery]):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    event = visible_event(access=access, event_id=event_id, allow_invitation=True)
    occurrences = _perform(
        recurrence.list_occurrences, event=event, start=filters.start, end=filters.end,
        timezone_name=filters.timezone, include_cancelled=filters.include_cancelled,
    )
    return {"items": [recurrence.occurrence_item(access, event, occurrence) for occurrence in occurrences]}


@router.get("/{org_id}/events/{event_id}/occurrences/{key}/", response=EventOutput)
def get_occurrence(request, org_id: RecordId, event_id: RecordId, key: date):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    event = visible_event(access=access, event_id=event_id, allow_invitation=True)
    return recurrence.occurrence_item(access, event, recurrence.resolve_occurrence(event, key))


@router.patch("/{org_id}/events/{event_id}/occurrences/{key}/", response=EventOutput)
def update_occurrence(request, org_id: RecordId, event_id: RecordId, key: date, payload: OccurrenceChange):
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    if changes.get("online_url") is not None:
        changes["online_url"] = str(changes["online_url"])
    return _perform(
        recurrence.update_occurrence, user=request.user, org_id=org_id, event_id=event_id, key=key,
        expected_version=payload.expected_version, changes=changes,
    )


@router.post("/{org_id}/events/{event_id}/occurrences/{key}/cancel/", response=EventOutput)
def cancel_occurrence(request, org_id: RecordId, event_id: RecordId, key: date, payload: OccurrenceVersion):
    return _perform(
        recurrence.cancel_occurrence, user=request.user, org_id=org_id, event_id=event_id, key=key,
        expected_version=payload.expected_version,
    )


@router.post("/{org_id}/events/{event_id}/recurrence/stop/", response=RecurrenceDetail)
def stop_recurrence(request, org_id: RecordId, event_id: RecordId, payload: StopRecurrenceInput):
    return _perform(
        recurrence.stop_recurrence, user=request.user, org_id=org_id, event_id=event_id,
        from_date=payload.from_date,
    )


@router.get("/{org_id}/events/{event_id}/recurrence/exceptions/", response=OccurrenceExceptions)
def list_exceptions(request, org_id: RecordId, event_id: RecordId, filters: Query[ExceptionPageQuery]):
    access = organization_access(user=request.user, org_id=org_id, module=CALENDAR)
    event = visible_event(access=access, event_id=event_id)
    rows = event.occurrence_exceptions.order_by("occurrence_key")
    offset = (filters.page - 1) * filters.page_size
    schedule = recurrence._schedule(event)
    occurrences = (
        recurrence._occurrence(event, schedule, exception.occurrence_key, exception)
        for exception in rows[offset:offset + filters.page_size]
    )
    return {
        "items": [
            recurrence.occurrence_item(access, event, occurrence)
            for occurrence in occurrences if occurrence is not None
        ],
        "page": filters.page,
        "page_size": filters.page_size,
        "total": rows.count(),
    }
