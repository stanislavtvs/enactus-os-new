from datetime import datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from apps.access.policy import Permission
from apps.organizations.models import Organization

from .models import (
    TASK_NAME_MAX_LENGTH,
    TASK_PRIORITY_CODE_MAX_LENGTH,
    TASK_PRIORITY_NAME_MAX_LENGTH,
    TASK_STATUS_CODE_MAX_LENGTH,
    TASK_STATUS_NAME_MAX_LENGTH,
    Task,
    TaskDependency,
)

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InputId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class TaskChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_NAME_MAX_LENGTH, pattern=r"^[^\x00]+$",
    )]
    description: Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")] = ""
    priority: Task.Priority = Task.Priority.NORMAL
    priority_id: InputId | None = None
    department_id: InputId | None = None
    planned_start_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None

    @field_validator("name", "description", "priority", "priority_id")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class SubtaskInput(TaskChangeInput):
    project_id: InputId | None = None
    assignee_id: InputId | None = None


class TaskInput(SubtaskInput):
    parent_id: InputId | None = None


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
    priority_id: RecordId | None = None
    workflow_status_id: RecordId | None = None
    sort: Literal["priority", "status"] | None = None
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


class PriorityLevelOutput(Schema):
    id: int
    code: str
    name: str
    rank: int
    color: str


class TaskPriorityOutput(PriorityLevelOutput):
    sort_order: int
    is_active: bool
    is_default: bool


class TaskPriorityListOutput(Schema):
    can_manage: bool
    items: list[TaskPriorityOutput]


class TaskPriorityChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_PRIORITY_NAME_MAX_LENGTH,
        pattern=r"^[^\x00]+$",
    )]
    rank: Annotated[int, Field(ge=-(2**31), le=2**31 - 1, strict=True)]
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$", to_lower=True)]
    sort_order: Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)] = 0
    is_default: Annotated[bool, Field(strict=True)] = False

    @field_validator("name", "rank", "color", "sort_order", "is_default")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class TaskPriorityInput(TaskPriorityChangeInput):
    code: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_PRIORITY_CODE_MAX_LENGTH,
        pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$",
    )]


class WorkflowStatusOutput(Schema):
    id: int
    code: str
    name: str
    stage: Task.Status
    color: str


class TaskStatusOutput(WorkflowStatusOutput):
    sort_order: int
    is_active: bool
    is_default: bool


class TaskStatusListOutput(Schema):
    can_manage: bool
    items: list[TaskStatusOutput]


class TaskStatusChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_STATUS_NAME_MAX_LENGTH,
        pattern=r"^[^\x00]+$",
    )]
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$", to_lower=True)]
    sort_order: Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)] = 0
    is_default: Annotated[bool, Field(strict=True)] = False

    @field_validator("name", "color", "sort_order", "is_default")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class TaskStatusInput(TaskStatusChangeInput):
    code: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_STATUS_CODE_MAX_LENGTH,
        pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$",
    )]
    stage: Task.Status


class TaskTransitionChangeInput(Schema):
    model_config = ConfigDict(extra="forbid")

    is_enabled: Annotated[bool, Field(strict=True)] = True
    allow_assignee: Annotated[bool, Field(strict=True)] = False
    required_permissions: Annotated[list[Permission], Field(max_length=len(Permission))] = Field(default_factory=list)

    @field_validator("is_enabled", "allow_assignee", "required_permissions")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class TaskTransitionInput(TaskTransitionChangeInput):
    from_status_id: InputId
    to_status_id: InputId


class TaskTransitionOutput(Schema):
    id: int
    from_status_id: int
    to_status_id: int
    is_enabled: bool
    allow_assignee: bool
    required_permissions: list[Permission]


class WorkflowPermissionOption(Schema):
    code: Permission
    label: str


class TaskTransitionListOutput(Schema):
    can_manage: bool
    items: list[TaskTransitionOutput]
    permission_options: list[WorkflowPermissionOption]
    stage_targets: dict[Task.Status, list[Task.Status]]


class TaskWorkflowStatusInput(Schema):
    model_config = ConfigDict(extra="forbid")

    status_id: InputId
    expected_status_id: InputId


class TaskWorkflowStatusOutput(Schema):
    current: WorkflowStatusOutput | None
    allowed_workflow_statuses: list[WorkflowStatusOutput]


class TaskOutput(Schema):
    id: int
    name: str
    description: str
    status: Task.Status
    priority: Task.Priority
    priority_level: PriorityLevelOutput | None
    workflow_status: WorkflowStatusOutput | None
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
    parent_id: int | None
    parent: NamedObject | None
    has_unfinished_children: bool
    can_view_decomposition: bool
    has_blocking_dependencies: bool
    has_dependency_warnings: bool
    permissions: TaskPermissions
    allowed_statuses: list[Task.Status]
    allowed_workflow_statuses: list[WorkflowStatusOutput]


class TaskListOutput(Schema):
    items: list[TaskOutput]
    total: int
    page: int
    page_size: int


class HierarchyQuery(Schema):
    page: Annotated[int, Field(ge=1, le=2**31 - 1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 50


class ParentInput(Schema):
    model_config = ConfigDict(extra="forbid")

    parent_id: InputId | None


class SubtaskPermissions(Schema):
    update: bool
    archive: bool


class SubtaskOutput(Schema):
    id: int
    parent_id: int | None
    name: str
    status: Task.Status
    workflow_status: WorkflowStatusOutput | None
    assignee: MemberOutput | None
    due_at: datetime | None
    is_archived: bool
    permissions: SubtaskPermissions
    can_move: bool


class SubtaskListOutput(Schema):
    items: list[SubtaskOutput]
    total: int
    completed: int
    page: int
    page_size: int


class ChecklistNameInput(Schema):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=TASK_NAME_MAX_LENGTH, pattern=r"^[^\x00]+$",
    )]

    @field_validator("name")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Поле не может быть пустым.")
        return value


class ChecklistItemChangeInput(ChecklistNameInput):
    is_completed: Annotated[bool, Field(strict=True)] = False

    @field_validator("is_completed")
    @classmethod
    def reject_null_completion(cls, value):
        if value is None:
            raise ValueError("Укажите состояние пункта.")
        return value


class ChecklistOrderInput(Schema):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[InputId]


class ConversionInput(Schema):
    model_config = ConfigDict(extra="forbid")

    confirm: Annotated[bool, Field(strict=True)]


class InverseConversionInput(ConversionInput):
    checklist_id: InputId


class ChecklistItemOutput(Schema):
    id: int
    name: str
    position: int
    is_completed: bool
    completed_at: datetime | None
    completed_by: MemberOutput | None


class ChecklistOutput(Schema):
    id: int
    name: str
    completed: int
    total: int
    items: list[ChecklistItemOutput]


class ChecklistConversionOutput(Schema):
    id: int
    direction: Literal["item_to_task", "task_to_item"]
    item_id: int
    item_name: str
    task_id: int | None
    task_name: str | None
    actor: MemberOutput
    created_at: datetime


class DecompositionOutput(Schema):
    can_manage: bool
    can_create_subtask: bool
    can_move: bool
    can_convert_to_item: bool
    convert_to_item_reason: str | None
    checklists: list[ChecklistOutput]
    conversions: list[ChecklistConversionOutput]


class DependencyInput(Schema):
    model_config = ConfigDict(extra="forbid")

    predecessor_id: InputId
    kind: TaskDependency.Kind


class DependencyPredecessor(NamedObject):
    status: Task.Status


class DependencyOutput(Schema):
    id: int
    kind: TaskDependency.Kind
    predecessor: DependencyPredecessor | None
    unresolved: bool


class DependencyListOutput(Schema):
    can_manage: bool
    blocked: bool
    items: list[DependencyOutput]


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
    priorities: list[TaskPriorityOutput]
    statuses: list[TaskStatusOutput]


class TaskAssigneeOptionsQuery(Schema):
    project_id: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    department_id: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    parent_id: RecordId | None = None


class TaskAssigneeOptions(Schema):
    assignment_mode: Organization.TaskAssignmentMode
    can_assign: bool
    candidates: list[MemberOutput]


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
