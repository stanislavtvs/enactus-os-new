from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_validator, model_validator

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class AuditQuery(Schema):
    q: str = Field("", max_length=120)
    actor_id: RecordId | None = None
    action: str | None = Field(None, max_length=80)
    object_type: str | None = Field(None, max_length=64)
    project_id: RecordId | None = None
    department_id: RecordId | None = None
    date_from: date | None = None
    date_to: date | None = None
    page: int = Field(1, ge=1, le=1_000_000)
    page_size: int = Field(25, ge=1, le=100)

    @field_validator("q", "action", "object_type")
    @classmethod
    def validate_text(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("Нулевой символ недопустим.")
        return value

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Начало диапазона должно предшествовать его окончанию.")
        return self


class AuditSummary(Schema):
    id: int
    action: str
    action_label: str
    object_type: str
    object_type_label: str
    object_id: int | None
    object_title: str | None
    object_url: str | None
    actor_id: int | None
    actor_name: str | None
    department_id: int | None
    department_name: str | None
    project_id: int | None
    project_name: str | None
    created_at: datetime
    can_view_details: bool


class AuditDetail(AuditSummary):
    changes: dict | None
    metadata: dict | None
    operation_id: UUID | None


class AuditPage(Schema):
    items: list[AuditSummary]
    total: int
    page: int
    page_size: int


class AuditEntityOption(Schema):
    id: int
    name: str


class AuditCodeOption(Schema):
    code: str
    label: str


class AuditOptions(Schema):
    actors: list[AuditEntityOption]
    projects: list[AuditEntityOption]
    departments: list[AuditEntityOption]
    actions: list[AuditCodeOption]
    object_types: list[AuditCodeOption]
