from datetime import date, datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .models import StaffingAllocation, StaffingRequest
from .schemas import InputId, LeadOutput

PositiveCount = Annotated[int, Field(gt=0, le=2**31 - 1, strict=True)]
Text = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid")


class RequestFields(StrictSchema):
    requested_count: PositiveCount
    description: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$",
    )]
    specialization: Annotated[Text, StringConstraints(max_length=200)] = ""
    desired_start: date | None = None
    duration_days: PositiveCount | None = None

    @field_validator("requested_count", "description", "specialization")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class RequestInput(RequestFields):
    department_id: InputId

    @field_validator("department_id")
    @classmethod
    def require_department(cls, value):
        if value is None:
            raise ValueError("Выберите подразделение.")
        return value


class AllocateInput(StrictSchema):
    department_membership_ids: Annotated[list[InputId], Field(min_length=1, max_length=100)]


class RejectInput(StrictSchema):
    comment: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$",
    )]


class InvitationDecision(StrictSchema):
    decision: Literal["accept", "decline"]


class AssignmentModeInput(StrictSchema):
    mode: Literal["direct", "invitation"]


class TeamMemberOutput(Schema):
    id: int
    username: str
    source_department_name: str | None
    is_active: bool
    joined_at: datetime
    ended_at: datetime | None


class TeamOutput(Schema):
    lead: LeadOutput | None
    members: list[TeamMemberOutput]
    can_view_team: bool
    can_view_requests: bool
    can_manage_team: bool
    can_request: bool


class AllocationOutput(Schema):
    id: int
    username: str
    department_name: str
    status: StaffingAllocation.Status
    mode: str
    created_at: datetime
    resolved_at: datetime | None
    allocated_by: str | None
    can_revoke: bool


class RequestOutput(Schema):
    id: int
    project_id: int
    project_name: str
    department_id: int
    department_name: str
    requested_count: int
    description: str
    specialization: str
    desired_start: date | None
    duration_days: int | None
    status: StaffingRequest.Status
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    handled_by: str | None
    handled_at: datetime | None
    comment: str
    accepted_count: int
    pending_count: int
    can_edit: bool
    can_cancel: bool
    can_allocate: bool
    awaiting_handler: bool
    history: list[AllocationOutput]


class CandidateOutput(Schema):
    id: int
    username: str
    department_name: str
    projects: list[str]


class InvitationOutput(Schema):
    id: int
    project_name: str
    organization_name: str
    department_name: str
    status: StaffingAllocation.Status
    created_at: datetime
