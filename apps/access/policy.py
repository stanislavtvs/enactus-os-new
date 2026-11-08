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
    ROLES_READ = "roles.read", "Просматривать роли"
    ROLES_MANAGE = "roles.manage", "Управлять ролями"


ORGANIZATION_ONLY = frozenset({
    Permission.ORGANIZATIONS_ONBOARDING_MANAGE, Permission.ROLES_READ, Permission.ROLES_MANAGE,
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
