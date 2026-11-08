from datetime import time
from typing import Annotated

from ninja import Schema
from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .models import NotificationEvent

KindCode = Annotated[str, StringConstraints(max_length=80)]
Offset = Annotated[int, Field(ge=0, le=1440, strict=True)]


class NotificationPolicyChange(Schema):
    model_config = ConfigDict(extra="forbid")

    enabled_categories: Annotated[list[NotificationEvent.Category], Field(max_length=5)] = Field(default_factory=list)
    disabled_kinds: Annotated[list[KindCode], Field(max_length=100)] = Field(default_factory=list)
    task_offsets_minutes: Annotated[list[Offset], Field(max_length=4)] = Field(default_factory=list)
    calendar_offsets_minutes: Annotated[list[Offset], Field(max_length=3)] = Field(default_factory=list)
    task_overdue: Annotated[bool, Field(strict=True)] = False
    include_collaborators: Annotated[bool, Field(strict=True)] = False
    calendar_informational: Annotated[bool, Field(strict=True)] = True
    all_day_reminder_time: time = time(9, 0)

    @field_validator("*")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value

    @field_validator("all_day_reminder_time")
    @classmethod
    def local_minute(cls, value):
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Укажите местное время в формате ЧЧ:ММ.")
        return value


class NotificationPolicy(Schema):
    enabled_categories: list[NotificationEvent.Category]
    disabled_kinds: list[str]
    task_offsets_minutes: list[int]
    calendar_offsets_minutes: list[int]
    task_overdue: bool
    include_collaborators: bool
    calendar_informational: bool
    all_day_reminder_time: time


class CategoryOption(Schema):
    code: NotificationEvent.Category
    label: str


class KindOption(Schema):
    code: str
    category: NotificationEvent.Category
    label: str


class NotificationPolicyResponse(Schema):
    policy: NotificationPolicy
    categories: list[CategoryOption]
    kinds: list[KindOption]
    task_offset_options: list[int]
    calendar_offset_options: list[int]


class NotificationPreferenceChange(Schema):
    model_config = ConfigDict(extra="forbid")

    enabled_categories: Annotated[list[NotificationEvent.Category], Field(max_length=5)] = Field(default_factory=list)
    task_reminders: Annotated[bool, Field(strict=True)] = True
    calendar_reminders: Annotated[bool, Field(strict=True)] = True
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] = "UTC"
    quiet_start: time | None = None
    quiet_end: time | None = None

    @field_validator("enabled_categories", "task_reminders", "calendar_reminders", "timezone")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def local_minute(cls, value):
        if value is not None and (value.tzinfo is not None or value.second or value.microsecond):
            raise ValueError("Укажите местное время в формате ЧЧ:ММ.")
        return value


class NotificationPreferences(Schema):
    enabled_categories: list[NotificationEvent.Category]
    task_reminders: bool
    calendar_reminders: bool
    timezone: str
    quiet_start: time | None
    quiet_end: time | None


class NotificationPreferenceResponse(Schema):
    preferences: NotificationPreferences
    categories: list[CategoryOption]
    policy_enabled_categories: list[NotificationEvent.Category]
