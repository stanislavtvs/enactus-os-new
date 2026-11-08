from collections import defaultdict
from functools import cached_property

from django.db import models
from django.db.models import Q


class Permission(models.TextChoices):
    DEPARTMENTS_READ = "departments.read", "Просматривать подразделения"
    DEPARTMENTS_CREATE = "departments.create", "Создавать подразделения"
    DEPARTMENTS_UPDATE = "departments.update", "Редактировать подразделения"
    DEPARTMENTS_MOVE = "departments.move", "Перемещать подразделения"
    DEPARTMENTS_ARCHIVE = "departments.archive", "Архивировать подразделения"
    DEPARTMENTS_DIRECTORY_READ = "departments.directory.read", "Просматривать каталог участников"
    DEPARTMENTS_INVITATIONS_MANAGE = "departments.invitations.manage", "Управлять приглашениями подразделения"
    DEPARTMENTS_MEMBERS_MANAGE = "departments.members.manage", "Управлять участниками подразделения"
    ORGANIZATIONS_ONBOARDING_MANAGE = "organizations.onboarding.manage", "Управлять вступлением в организацию"
    PROJECTS_VIEW = "projects.view", "Просматривать проекты"
    PROJECTS_CREATE = "projects.create", "Создавать проекты"
    PROJECTS_UPDATE = "projects.update", "Редактировать проекты"
    PROJECTS_ARCHIVE = "projects.archive", "Архивировать проекты"
    PROJECTS_MANAGE_DEPARTMENTS = "projects.manage_departments", "Настраивать подразделения проекта"
    PROJECTS_MANAGE_LEAD = "projects.manage_lead", "Назначать руководителя проекта"
    PROJECTS_TEAM_VIEW = "projects.team.view", "Просматривать команду проекта"
    PROJECTS_TEAM_MANAGE = "projects.team.manage", "Управлять членством в проекте"
    PROJECTS_STAFFING_REQUEST = "projects.staffing.request", "Запрашивать специалистов для своего проекта"
    DEPARTMENTS_STAFFING_ALLOCATE = "departments.staffing.allocate", "Распределять специалистов подразделения"
    TASKS_VIEW = "tasks.view", "Просматривать задачи"
    TASKS_CREATE = "tasks.create", "Создавать задачи"
    TASKS_UPDATE = "tasks.update", "Редактировать задачи"
    TASKS_ASSIGN = "tasks.assign", "Назначать исполнителей задач"
    TASKS_CHANGE_STATUS = "tasks.change_status", "Менять статус задач"
    TASKS_ARCHIVE = "tasks.archive", "Архивировать задачи"
    TASKS_ROUTE = "tasks.route", "Направлять задачи на распределение"
    TASKS_DISTRIBUTION_VIEW = "tasks.distribution.view", "Просматривать поручения на распределение"
    TASKS_DISTRIBUTE = "tasks.distribute", "Распределять исполнителей задач"
    TASKS_REVIEW_SUBMIT = "tasks.review.submit", "Отправлять результат задачи на проверку"
    TASKS_REVIEW_APPROVE = "tasks.review.approve", "Принимать результат задачи"
    TASKS_REVIEW_RETURN = "tasks.review.return", "Возвращать задачу на доработку"
    TASKS_REVIEW_MANAGE = "tasks.review.manage", "Управлять правилами проверки задач"
    TASKS_WORKFLOW_VIEW = "tasks.workflow.view", "Просматривать настройки рабочих процессов"
    TASKS_PRIORITIES_MANAGE = "tasks.priorities.manage", "Управлять приоритетами задач"
    TASKS_STATUSES_MANAGE = "tasks.statuses.manage", "Управлять пользовательскими статусами задач"
    TASKS_TRANSITIONS_MANAGE = "tasks.transitions.manage", "Управлять переходами задач"
    TASKS_STATUS_EXECUTE = "tasks.status.execute", "Менять статус назначенной задачи по доступному переходу"
    CALENDAR_READ = "calendar.read", "Просматривать события календаря"
    CALENDAR_CREATE = "calendar.create", "Создавать события календаря"
    CALENDAR_UPDATE = "calendar.update", "Редактировать события календаря"
    CALENDAR_CANCEL = "calendar.cancel", "Отменять события календаря"
    CALENDAR_ARCHIVE = "calendar.archive", "Архивировать события календаря"
    CALENDAR_MANAGE_SCOPE = "calendar.manage_scope", "Управлять событиями проектов и подразделений"
    ROLES_READ = "roles.read", "Просматривать роли"
    ROLES_MANAGE = "roles.manage", "Управлять ролями"


ORGANIZATION_ONLY = frozenset({
    Permission.ORGANIZATIONS_ONBOARDING_MANAGE, Permission.ROLES_READ, Permission.ROLES_MANAGE,
    Permission.TASKS_WORKFLOW_VIEW, Permission.TASKS_PRIORITIES_MANAGE,
    Permission.TASKS_STATUSES_MANAGE, Permission.TASKS_TRANSITIONS_MANAGE,
})
LEGACY_MEMBER_MANAGER = frozenset({
    Permission.DEPARTMENTS_CREATE,
    Permission.DEPARTMENTS_UPDATE,
    Permission.DEPARTMENTS_MOVE,
    Permission.DEPARTMENTS_ARCHIVE,
    Permission.DEPARTMENTS_DIRECTORY_READ,
    Permission.DEPARTMENTS_INVITATIONS_MANAGE,
    Permission.DEPARTMENTS_MEMBERS_MANAGE,
    Permission.ORGANIZATIONS_ONBOARDING_MANAGE,
})


class Access:
    def __init__(self, user, organization):
        self.user = user
        self.organization = organization

    @cached_property
    def membership(self):
        if self.user.is_authenticated and self.user.is_active:
            return self.organization.memberships.filter(user=self.user, is_active=True).first()
        return None

    @property
    def is_owner(self):
        return self.membership is not None and self.user.pk == self.organization.owner_id

    @cached_property
    def _grants(self):
        grants = defaultdict(dict)
        assignments = self.membership.role_assignments.filter(
            organization=self.organization, role__organization=self.organization,
            revoked_at__isnull=True, role__is_archived=False,
        ).filter(
            Q(department_membership__isnull=True)
            | Q(
                department_membership__is_active=True,
                department_membership__organization=self.organization,
                department_membership__organization_membership=self.membership,
                department_membership__department__organization=self.organization,
                department_membership__department__is_archived=False,
            )
        )
        for code, include_descendants, department_id in assignments.filter(
            role__permissions__isnull=False,
        ).values_list(
            "role__permissions__code",
            "role__permissions__include_descendants",
            "department_membership__department_id",
        ):
            scopes = grants[code]
            scopes[department_id] = scopes.get(department_id, False) or include_descendants
        return grants

    @cached_property
    def _parents(self):
        return dict(self.organization.departments.filter(is_archived=False).values_list("id", "parent_id"))

    def allows(self, action, department=None, include_descendants=False):
        if self.membership is None or action not in Permission.values:
            return False
        if department is not None and (
            department.organization_id != self.organization.pk
            or department.is_archived
            or action in ORGANIZATION_ONLY
        ):
            return False
        if self.is_owner or action == Permission.DEPARTMENTS_READ:
            return True
        if self.membership.can_manage_members and action in LEGACY_MEMBER_MANAGER:
            return True

        scopes = self._grants.get(action, {})
        if None in scopes:
            return True
        if department is None:
            return False
        if department.pk in scopes and (not include_descendants or scopes[department.pk]):
            return True

        parent_id = self._parents.get(department.pk)
        while parent_id is not None:
            if scopes.get(parent_id):
                return True
            parent_id = self._parents.get(parent_id)
        return False


def can(user, action, organization, department=None, include_descendants=False):
    return Access(user, organization).allows(action, department, include_descendants)
