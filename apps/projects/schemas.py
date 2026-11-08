from datetime import date, datetime
from typing import Annotated

from ninja import Schema
from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .models import PROJECT_NAME_MAX_LENGTH, Project

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InputId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class ProjectChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=PROJECT_NAME_MAX_LENGTH, pattern=r"^[^\x00]+$",
    )]
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] = ""
    status: Project.Status = Project.Status.DRAFT
    start_date: date | None = None
    planned_end_date: date | None = None

    @field_validator("name", "description", "status")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class ProjectInput(ProjectChangeInput):
    lead_membership_id: InputId | None = None
    responsible_department_id: InputId | None = None


class LeadInput(Schema):
    model_config = ConfigDict(extra="forbid")

    lead_membership_id: InputId | None


class ResponsibleDepartmentInput(Schema):
    model_config = ConfigDict(extra="forbid")

    responsible_department_id: InputId | None


class DepartmentOutput(Schema):
    id: int
    name: str
    is_archived: bool


class LeadOutput(Schema):
    id: int
    username: str


class ProjectPermissions(Schema):
    update: bool
    archive: bool
    manage_departments: bool
    manage_lead: bool


class ProjectOutput(Schema):
    id: int
    name: str
    description: str
    status: Project.Status
    start_date: date | None
    planned_end_date: date | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    lead: LeadOutput | None
    responsible_department: DepartmentOutput | None
    participating_departments: list[DepartmentOutput]
    permissions: ProjectPermissions


class DepartmentOption(Schema):
    id: int
    name: str
    can_create: bool
    can_manage_departments: bool
    can_manage_lead: bool
    can_set_responsible: bool


class ProjectOptions(Schema):
    can_create_independent: bool
    departments: list[DepartmentOption]
    leaders: list[LeadOutput]
    can_manage_independent: bool
    can_manage_independent_lead: bool
    can_set_independent: bool
