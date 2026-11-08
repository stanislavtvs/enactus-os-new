from django.db import models
from django.db.models import F, Q

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.projects.models import Project

TASK_NAME_MAX_LENGTH = 200
TASK_PRIORITY_CODE_MAX_LENGTH = 64
TASK_PRIORITY_NAME_MAX_LENGTH = 120
TASK_STATUS_CODE_MAX_LENGTH = 64
TASK_STATUS_NAME_MAX_LENGTH = 120


class TaskPriority(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="task_priorities")
    code = models.SlugField(max_length=TASK_PRIORITY_CODE_MAX_LENGTH)
    name = models.CharField(max_length=TASK_PRIORITY_NAME_MAX_LENGTH)
    rank = models.IntegerField()
    color = models.CharField(max_length=7)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="unique_task_priority_code",
            ),
            models.UniqueConstraint(
                fields=["organization", "id"], name="unique_task_priority_organization_id",
            ),
            models.UniqueConstraint(
                fields=["organization"], condition=Q(is_default=True), name="unique_task_priority_default",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True), name="task_priority_default_is_active",
            ),
        ]


class Task(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        IN_PROGRESS = "in_progress", "В работе"
        REVIEW = "review", "На проверке"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    class Priority(models.TextChoices):
        LOW = "low", "Низкий"
        NORMAL = "normal", "Обычный"
        HIGH = "high", "Высокий"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=TASK_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="authored_tasks",
    )
    assignee = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="assigned_tasks",
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="subtasks",
    )
    planned_start_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=11, choices=Status, default=Status.PENDING)
    workflow_status = models.ForeignKey(
        "TaskStatus", on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    priority = models.CharField(max_length=6, choices=Priority, default=Priority.NORMAL)
    priority_level = models.ForeignKey(
        TaskPriority, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks",
    )
    is_archived = models.BooleanField(default=False)
    review_policy = models.CharField(
        max_length=18, choices=Organization.TaskReviewPolicy, null=True, blank=True,
    )
    reviewer = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="explicitly_reviewed_tasks",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["organization", "due_at"],
                condition=Q(due_at__isnull=False),
                name="task_org_calendar_due_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="unique_task_organization_id"),
            models.CheckConstraint(condition=~Q(parent_id=F("id")), name="task_parent_is_not_self"),
            models.CheckConstraint(
                condition=Q(planned_start_at__isnull=True) | Q(due_at__isnull=True)
                | Q(due_at__gte=F("planned_start_at")),
                name="task_due_not_before_start",
            ),
            models.CheckConstraint(
                condition=Q(status="completed", completed_at__isnull=False)
                | (~Q(status="completed") & Q(completed_at__isnull=True)),
                name="task_completed_timestamp_matches_status",
            ),
        ]


class TaskStatus(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="task_statuses")
    code = models.SlugField(max_length=TASK_STATUS_CODE_MAX_LENGTH)
    name = models.CharField(max_length=TASK_STATUS_NAME_MAX_LENGTH)
    stage = models.CharField(max_length=11, choices=Task.Status)
    color = models.CharField(max_length=7)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="unique_task_status_code"),
            models.UniqueConstraint(fields=["organization", "id"], name="unique_task_status_organization_id"),
            models.UniqueConstraint(
                fields=["organization", "stage", "id"], name="unique_task_status_organization_stage_id",
            ),
            models.UniqueConstraint(
                fields=["organization", "stage"], condition=Q(is_default=True),
                name="unique_task_status_stage_default",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True), name="task_status_default_is_active",
            ),
        ]


class TaskStatusTransition(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="task_status_transitions")
    from_status = models.ForeignKey(TaskStatus, on_delete=models.PROTECT, related_name="outgoing_transitions")
    to_status = models.ForeignKey(TaskStatus, on_delete=models.PROTECT, related_name="incoming_transitions")
    is_enabled = models.BooleanField(default=True)
    allow_assignee = models.BooleanField(default=False)
    required_permissions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["from_status", "to_status"], name="unique_task_status_transition_pair",
            ),
            models.CheckConstraint(
                condition=~Q(from_status_id=F("to_status_id")), name="task_status_transition_is_not_self",
            ),
        ]


class TaskCollaborator(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="task_collaborators",
    )
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="collaborators")
    organization_membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="collaborating_tasks",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "organization_membership"], name="unique_task_collaborator",
            ),
        ]


class TaskDistribution(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает распределения"
        ASSIGNED = "assigned", "Назначено"
        CANCELLED = "cancelled", "Отменено"
        CLEARED = "cleared", "Назначение снято"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="distributions")
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    requested_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="requested_task_distributions",
    )
    assignee = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="received_task_distributions",
    )
    resolved_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
        related_name="resolved_task_distributions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    mode = models.CharField(max_length=10, choices=Organization.TaskAssignmentMode)
    status = models.CharField(max_length=9, choices=Status, default=Status.PENDING)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task"], condition=Q(status="pending"), name="unique_pending_task_distribution",
            ),
        ]


class TaskReviewSubmission(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="review_submissions")
    submitted_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, related_name="submitted_task_reviews",
    )
    policy = models.CharField(max_length=18, choices=Organization.TaskReviewPolicy)
    comment = models.TextField(blank=True, default="")
    links = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_review_submission_scope",
            ),
        ]


class TaskReviewStage(models.Model):
    class Kind(models.TextChoices):
        DEPARTMENT = "department", "Руководитель подразделения"
        PROJECT = "project", "Руководитель проекта"
        EXPLICIT = "explicit", "Назначенный проверяющий"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    submission = models.ForeignKey(TaskReviewSubmission, on_delete=models.CASCADE, related_name="stages")
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=10, choices=Kind)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    reviewer = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "position"], name="unique_task_review_stage_position"),
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_review_stage_scope",
            ),
            models.CheckConstraint(condition=Q(position__gt=0), name="task_review_stage_position_positive"),
        ]


class TaskReviewDecision(models.Model):
    class Outcome(models.TextChoices):
        APPROVED = "approved", "Принято"
        RETURNED = "returned", "Возвращено на доработку"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    stage = models.OneToOneField(TaskReviewStage, on_delete=models.CASCADE, related_name="decision")
    actor = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=8, choices=Outcome)
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class Checklist(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklists")
    name = models.CharField(max_length=TASK_NAME_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_checklist_scope",
            ),
        ]


class ChecklistItem(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist_items")
    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=TASK_NAME_MAX_LENGTH)
    position = models.PositiveIntegerField()
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        OrganizationMembership, on_delete=models.PROTECT, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "task", "id"], name="unique_task_checklist_item_scope",
            ),
            models.UniqueConstraint(
                fields=["checklist", "position"], condition=Q(archived_at__isnull=True),
                name="unique_active_checklist_item_position",
            ),
            models.CheckConstraint(condition=Q(position__gt=0), name="checklist_item_position_positive"),
            models.CheckConstraint(
                condition=Q(completed_at__isnull=True, completed_by__isnull=True)
                | Q(completed_at__isnull=False, completed_by__isnull=False),
                name="checklist_item_completion_pair",
            ),
        ]

    @property
    def is_completed(self):
        return self.completed_at is not None


class ChecklistConversion(models.Model):
    class Direction(models.TextChoices):
        ITEM_TO_TASK = "item_to_task", "Пункт в подзадачу"
        TASK_TO_ITEM = "task_to_item", "Подзадача в пункт"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    parent_task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="child_conversions")
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="checklist_conversions")
    item = models.ForeignKey(ChecklistItem, on_delete=models.PROTECT, related_name="conversions")
    actor = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT)
    direction = models.CharField(max_length=12, choices=Direction)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item"], condition=Q(direction="item_to_task"),
                name="unique_checklist_item_forward_conversion",
            ),
            models.UniqueConstraint(
                fields=["task"], condition=Q(direction="task_to_item"),
                name="unique_task_inverse_conversion",
            ),
        ]


class TaskDependency(models.Model):
    class Kind(models.TextChoices):
        INFORMATIONAL = "informational", "Предупреждение"
        STRICT = "strict", "Блокирует выполнение"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    predecessor = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="dependents")
    kind = models.CharField(max_length=13, choices=Kind)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "predecessor"], name="unique_task_dependency_pair",
            ),
            models.CheckConstraint(
                condition=~Q(task_id=F("predecessor_id")), name="task_dependency_is_not_self",
            ),
        ]
