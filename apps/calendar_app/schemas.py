from datetime import date, datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from .models import EVENT_LOCATION_MAX_LENGTH, EVENT_TITLE_MAX_LENGTH, CalendarEvent
from .recurrence_schemas import RecurrenceInput, RecurrenceOutput

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InputId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
TimezoneName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
CalendarSource = Literal["task_deadline", "meeting", "event"]


class EventQuery(Schema):
    start: date
    end: date
    timezone: TimezoneName = "UTC"
    source: CalendarEvent.Type | None = None
    project_id: RecordId | None = None
    department_id: RecordId | None = None
    include_archived: bool = False
    include_cancelled: bool = False


class CalendarQuery(EventQuery):
    source: CalendarSource | None = None


class EventInput(Schema):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=EVENT_TITLE_MAX_LENGTH, pattern=r"^[^\x00]+$",
    )]
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] = ""
    type: CalendarEvent.Type
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    timezone: TimezoneName
    all_day: Annotated[bool, Field(strict=True)] = False
    rsvp_mode: CalendarEvent.RSVPMode = CalendarEvent.RSVPMode.INFORMATIONAL
    location: Annotated[str, StringConstraints(
        strip_whitespace=True, max_length=EVENT_LOCATION_MAX_LENGTH, pattern=r"^[^\x00]*$",
    )] | None = None
    online_url: HttpUrl | Literal[""] | None = None
    project_id: InputId | None = None
    department_id: InputId | None = None
    recurrence: RecurrenceInput | None = None

    @field_validator("title", "description", "type", "starts_at", "ends_at", "timezone", "all_day", "rsvp_mode")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value

    @field_validator("online_url")
    @classmethod
    def check_normalized_url_length(cls, value):
        if value and len(str(value)) > 2048:
            raise ValueError("Ссылка не может быть длиннее 2048 символов.")
        return value


class CalendarItem(Schema):
    id: int
    source: CalendarSource
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime | None
    timezone: str
    all_day: bool
    status: str
    status_label: str
    project_id: int | None
    department_id: int | None
    location: str | None
    online_url: str | None
    url: str | None
    can_edit: bool
    can_cancel: bool
    can_archive: bool
    series_id: int | None = None
    occurrence_key: date | None = None
    original_starts_at: datetime | None = None
    original_ends_at: datetime | None = None
    is_recurring: bool = False
    is_exception: bool = False
    occurrence_version: int | None = None
    occurrence_rsvp_revision: int | None = None
    recurrence: RecurrenceOutput | None = None


class EventOutput(CalendarItem):
    type: CalendarEvent.Type
    can_manage_scope: bool
    created_at: datetime
    updated_at: datetime
    rsvp_mode: CalendarEvent.RSVPMode
    rsvp_revision: int
    invitation_id: int | None
    can_invite: bool
    can_manage_invitations: bool


class CalendarOutput(Schema):
    items: list[CalendarItem]


class EventListOutput(Schema):
    items: list[EventOutput]


class CalendarScopeOption(Schema):
    id: int
    name: str
    can_create: bool


class CalendarFilters(Schema):
    projects: list[CalendarScopeOption]
    departments: list[CalendarScopeOption]
    can_create: bool
    can_create_organization: bool
