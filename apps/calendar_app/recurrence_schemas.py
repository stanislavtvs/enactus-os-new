from datetime import date
from typing import Annotated, Literal

from ninja import Schema
from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from .recurrence_models import MAX_RECURRENCE_COUNT, MAX_RECURRENCE_INTERVAL, CalendarRecurrenceRule

TimezoneName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class RecurrenceInput(Schema):
    model_config = ConfigDict(extra="forbid")

    frequency: CalendarRecurrenceRule.Frequency
    interval: Annotated[int, Field(ge=1, le=MAX_RECURRENCE_INTERVAL, strict=True)] = 1
    weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7, strict=True)]], Field(max_length=7)] = Field(default_factory=list)
    until: date | None = None
    count: Annotated[int, Field(ge=1, le=MAX_RECURRENCE_COUNT, strict=True)] | None = None


class RecurrenceOutput(RecurrenceInput):
    stopped_from: date | None


class RecurrenceChange(Schema):
    model_config = ConfigDict(extra="forbid")

    recurrence: RecurrenceInput | None


class RecurrenceDetail(Schema):
    recurrence: RecurrenceOutput | None
    can_edit: bool


class OccurrenceQuery(Schema):
    start: date
    end: date
    timezone: TimezoneName = "UTC"
    include_cancelled: bool = False


class OccurrenceVersion(Schema):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]


class OccurrenceChange(OccurrenceVersion):
    title: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$",
    )] | None = None
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    location: Annotated[str, StringConstraints(
        strip_whitespace=True, max_length=240, pattern=r"^[^\x00]*$",
    )] | None = None
    online_url: HttpUrl | Literal[""] | None = None

    @field_validator("online_url")
    @classmethod
    def check_normalized_url_length(cls, value):
        if value and len(str(value)) > 2048:
            raise ValueError("Ссылка не может быть длиннее 2048 символов.")
        return value


class StopRecurrenceInput(Schema):
    model_config = ConfigDict(extra="forbid")

    from_date: date


class ExceptionPageQuery(Schema):
    page: Annotated[int, Field(ge=1, le=2**31 - 1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 50
