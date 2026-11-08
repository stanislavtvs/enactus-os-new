from datetime import datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from apps.organizations.models import Organization

from .models import TASK_NAME_MAX_LENGTH, Task

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InputId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class TaskChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_NAME_MAX_LENGTH, pattern=r"^[^\x00]+$",
    )]
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] = ""
    priority: Task.Priority = Task.Priority.NORMAL
    department_id: InputId | None = None
    planned_start_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None

    @field_validator("name", "description", "priority")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class TaskInput(TaskChangeInput):
    project_id: InputId | None = None


class AssigneeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    assignee_id: InputId | None


class StatusInput(Schema):
    model_config = ConfigDict(extra="forbid")

    status: Task.Status


class TaskListQuery(Schema):
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200, pattern=r"^[^\x00]*$")] = ""
    status: Task.Status | None = None
    project_id: RecordId | None = None
    department_id: RecordId | None = None
    mine: bool = False
    include_archived: bool = False
    distribution_pending: bool | None = None
    queue: Literal["distribution", "review"] | None = None
    page: Annotated[int, Field(ge=1, le=2**31 - 1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20


class MemberOutput(Schema):
    id: int
    username: str


class NamedObject(Schema):
    id: int
    name: str


class TaskPermissions(Schema):
    update: bool
    assign: bool
    archive: bool
    change_status: bool


class TaskOutput(Schema):
    id: int
    name: str
    description: str
    status: Task.Status
    priority: Task.Priority
    planned_start_at: datetime | None
    due_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    distribution_pending: bool
    author: MemberOutput
    assignee: MemberOutput | None
    collaborators: list[MemberOutput]
    project: NamedObject | None
    department: NamedObject | None
    permissions: TaskPermissions
    allowed_statuses: list[Task.Status]


class TaskListOutput(Schema):
    items: list[TaskOutput]
    total: int
    page: int
    page_size: int


class DepartmentOption(NamedObject):
    can_create: bool
    can_update: bool


class ProjectOption(NamedObject):
    can_create: bool
    department_ids: list[int]


class TaskOptions(Schema):
    assignment_mode: Organization.TaskAssignmentMode
    current_membership_id: int
    can_create_standalone: bool
    can_clear_department: bool
    departments: list[DepartmentOption]
    projects: list[ProjectOption]
    assignees: list[MemberOutput]


ReviewPolicy = Literal["project", "department", "department_project"]
ReviewOutcome = Literal["approved", "returned"]
WorkflowComment = Annotated[str, StringConstraints(
    strip_whitespace=True, max_length=5000, pattern=r"^[^\x00]*$",
)]


class ReviewPolicyInput(Schema):
    model_config = ConfigDict(extra="forbid")

    policy: ReviewPolicy | None


class TaskReviewPolicyInput(ReviewPolicyInput):
    reviewer_id: InputId | None = None


class ReviewSubmissionInput(Schema):
    model_config = ConfigDict(extra="forbid")

    comment: WorkflowComment = ""
    links: Annotated[list[HttpUrl], Field(max_length=10)] = Field(default_factory=list)


class ReviewDecisionInput(Schema):
    model_config = ConfigDict(extra="forbid")

    stage_id: InputId
    outcome: ReviewOutcome
    comment: WorkflowComment = ""


class ReviewPolicyOutput(Schema):
    policy: ReviewPolicy | None
    can_manage: bool


class DistributionEntryOutput(Schema):
    status: str
    mode: str
    department: NamedObject | None
    assignee: MemberOutput | None
    requested_by: MemberOutput
    resolved_by: MemberOutput | None
    created_at: datetime
    resolved_at: datetime | None


class DistributionOutput(Schema):
    pending: bool
    can_route: bool
    can_allocate: bool
    candidates: list[MemberOutput]
    history: list[DistributionEntryOutput]


class ReviewDecisionOutput(Schema):
    outcome: ReviewOutcome
    comment: str
    actor: MemberOutput
    created_at: datetime


class ReviewStageOutput(Schema):
    id: int
    kind: str
    position: int
    decision: ReviewDecisionOutput | None


class ReviewSubmissionOutput(Schema):
    id: int
    comment: str
    links: list[str]
    created_at: datetime
    submitted_by: MemberOutput
    stages: list[ReviewStageOutput]


class ReviewOutput(Schema):
    policy: ReviewPolicy
    override: ReviewPolicy | None
    reviewer_id: int | None
    can_manage: bool
    reviewers: list[MemberOutput]
    can_submit: bool
    can_approve: bool
    can_return: bool
    current_stage_id: int | None
    current_stage_kind: str | None
    submissions: list[ReviewSubmissionOutput]


class WorkflowOutput(Schema):
    distribution: DistributionOutput
    review: ReviewOutput
