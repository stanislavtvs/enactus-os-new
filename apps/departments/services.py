from collections import defaultdict

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.policy import Access
from apps.organizations.models import Organization
from apps.organizations.services import visible_organizations

from .models import DepartmentMembership

DEPARTMENT_MUTATIONS = ("create", "update", "move", "archive")


class DepartmentConflict(Exception):
    """The requested change conflicts with the current department tree."""


def visible_departments(*, user, org_id):
    organization = get_object_or_404(visible_organizations(user), pk=org_id)
    if not Access(user, organization).allows("departments.read"):
        raise PermissionDenied
    return organization.departments.order_by("id")


def active_department_members(organization):
    return DepartmentMembership.objects.filter(
        organization=organization,
        department__is_archived=False,
        is_active=True,
        organization_membership__is_active=True,
        organization_membership__user__is_active=True,
    )


def can_view_directory_others(access):
    return access.membership is not None and (
        access.organization.department_directory_visibility
        == Organization.DepartmentDirectoryVisibility.ORGANIZATION
        or access.allows("departments.directory.read")
    )


def can_view_department_directory(access, department):
    return (
        department.organization_id == access.organization.pk
        and not department.is_archived
        and (
            can_view_directory_others(access) or access.allows("departments.directory.read", department)
        )
    )


def _managed_departments(user, org_id):
    organizations = visible_organizations(user).select_for_update(of=("self",))
    organization = get_object_or_404(organizations, pk=org_id)
    return organization.departments, Access(user, organization)


def _require(access, action, department=None):
    if not access.allows(action, department):
        raise PermissionDenied


def _get_parent(departments, parent_id, *, department_id=None):
    if parent_id is None:
        return None

    parent = get_object_or_404(departments, pk=parent_id)
    if parent.is_archived:
        raise DepartmentConflict("Нельзя выбрать архивное подразделение родителем.")

    if department_id is not None:
        parents = dict(departments.values_list("id", "parent_id"))
        ancestors = {department_id}
        ancestor_id = parent.pk
        while ancestor_id is not None:
            if ancestor_id in ancestors:
                raise DepartmentConflict("Такое перемещение создаёт цикл в дереве.")
            ancestors.add(ancestor_id)
            ancestor_id = parents[ancestor_id]

    return parent


@transaction.atomic
def create_department(*, user, org_id, name, description="", parent_id=None):
    departments, access = _managed_departments(user, org_id)
    parent = _get_parent(departments, parent_id)
    _require(access, "departments.create", parent)
    return departments.create(name=name, description=description, parent=parent)


@transaction.atomic
def update_department(*, user, org_id, department_id, changes):
    departments, access = _managed_departments(user, org_id)
    department = get_object_or_404(departments, pk=department_id)
    if department.is_archived:
        _require(access, "departments.update")
        raise DepartmentConflict("Архивное подразделение нельзя изменить.")

    if not changes or {"name", "description"} & changes.keys():
        _require(access, "departments.update", department)
    if "parent_id" in changes:
        _require(access, "departments.move", department)
        parent = _get_parent(departments, changes["parent_id"], department_id=department.pk)
        _require(access, "departments.move", parent)
        department.parent = parent
    for field in ("name", "description"):
        if field in changes:
            setattr(department, field, changes[field])

    if changes:
        department.save(update_fields=[*changes, "updated_at"])
    return department


@transaction.atomic
def archive_department(*, user, org_id, department_id):
    departments, access = _managed_departments(user, org_id)
    department = get_object_or_404(departments, pk=department_id)
    if department.is_archived:
        _require(access, "departments.archive")
        return

    _require(access, "departments.archive", department)
    children = defaultdict(list)
    for child_id, parent_id in departments.values_list("id", "parent_id"):
        children[parent_id].append(child_id)

    subtree = {department.pk}
    pending = [department.pk]
    while pending:
        unseen = set(children[pending.pop()]) - subtree
        subtree.update(unseen)
        pending.extend(unseen)

    active_subtree = departments.filter(pk__in=subtree, is_archived=False)
    for node in active_subtree:
        _require(access, "departments.archive", node)

    active_subtree.update(
        is_archived=True, updated_at=timezone.now()
    )


def department_actions(*, user, org_id):
    organization = get_object_or_404(visible_organizations(user), pk=org_id)
    access = Access(user, organization)
    nodes = {node.pk: node for node in organization.departments.filter(is_archived=False)}
    actor_branches = set()
    if not can_view_directory_others(access):
        direct_ids = active_department_members(organization).filter(
            organization_membership=access.membership,
        ).values_list("department_id", flat=True)
        for node_id in direct_ids:
            while node_id in nodes and node_id not in actor_branches:
                actor_branches.add(node_id)
                node_id = nodes[node_id].parent_id
    actions = {
        node.pk: {
            **{
                action: access.allows(f"departments.{action}", node)
                for action in DEPARTMENT_MUTATIONS
            },
            "open_members": (
                node.pk in actor_branches or can_view_department_directory(access, node)
            ),
        }
        for node in nodes.values()
    }
    blocked = [pk for pk, permissions in actions.items() if not permissions["archive"]]
    while blocked:
        parent_id = nodes[blocked.pop()].parent_id
        if parent_id is not None and actions[parent_id]["archive"]:
            actions[parent_id]["archive"] = False
            blocked.append(parent_id)
    return {
        "root": {action: access.allows(f"departments.{action}") for action in ("create", "move")},
        "nodes": actions,
    }
