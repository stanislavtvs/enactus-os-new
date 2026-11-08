from collections import defaultdict

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.policy import Access
from apps.audit.services import record
from apps.organizations.services import visible_organizations

from .models import DepartmentInvitation, DepartmentMembership
from .services import (
    DepartmentConflict,
    active_department_members,
    can_view_department_directory,
    can_view_directory_others,
)


def _organization(user, org_id, *, lock=False):
    organizations = visible_organizations(user)
    if lock:
        organizations = organizations.select_for_update(of=("self",))
    return get_object_or_404(organizations, pk=org_id)


def _department(organization, department_id, *, lock=False):
    departments = organization.departments.filter(is_archived=False)
    if lock:
        departments = departments.select_for_update()
    return get_object_or_404(departments, pk=department_id)


def _managed_department(user, org_id, department_id, action, *, lock=False):
    organization = _organization(user, org_id, lock=lock)
    department = _department(organization, department_id, lock=lock)
    if not Access(user, organization).allows(action, department):
        raise PermissionDenied
    return department


def _active_org_members(organization):
    return organization.memberships.filter(is_active=True, user__is_active=True)


def _active_branch_members(department, branch_ids):
    return active_department_members(department.organization).filter(
        department_id__in=branch_ids,
    )


def _department_tree(organization):
    nodes = {
        node_id: (parent_id, name)
        for node_id, parent_id, name in organization.departments.filter(is_archived=False)
        .values_list("id", "parent_id", "name")
    }
    children = defaultdict(list)
    for node_id, (parent_id, _) in nodes.items():
        children[parent_id].append(node_id)
    return nodes, children


def _branch_ids(department_id, children):
    result = {department_id}
    pending = [department_id]
    while pending:
        unseen = set(children[pending.pop()]) - result
        result.update(unseen)
        pending.extend(unseen)
    return result


def _directory_context(user, org_id, department_id):
    organization = _organization(user, org_id)
    department = _department(organization, department_id)
    nodes, children = _department_tree(organization)
    if department_id not in nodes:
        raise Http404
    branch_members = _active_branch_members(department, _branch_ids(department_id, children))
    access = Access(user, organization)
    if not can_view_department_directory(access, department) and not branch_members.filter(
        organization_membership=access.membership,
    ).exists():
        raise Http404
    return department, nodes, branch_members, can_view_directory_others(access)


def get_directory_department(*, user, org_id, department_id):
    department, _, _, _ = _directory_context(user, org_id, department_id)
    return department


def _full_path(department_id, nodes):
    names = []
    while department_id is not None:
        department_id, name = nodes[department_id]
        names.append(name)
    names.reverse()
    return names


def department_directory(*, user, org_id, department_id, scope="branch"):
    department, nodes, branch_members, can_see_others = _directory_context(
        user, org_id, department_id
    )
    if scope == "others":
        if not can_see_others:
            return []
        accounts = _active_org_members(department.organization).exclude(
            pk__in=branch_members.values("organization_membership_id"),
        ).order_by("id").values_list("id", "user__username")
        return [
            {"organization_membership_id": member_id, "username": username, "memberships": []}
            for member_id, username in accounts
        ]

    if scope == "direct":
        direct_member_ids = branch_members.filter(
            department=department,
        ).values("organization_membership_id")
        branch_members = branch_members.filter(organization_membership_id__in=direct_member_ids)

    accounts = {}
    paths = {}
    for membership in branch_members.select_related("organization_membership__user").order_by(
        "organization_membership_id", "department_id"
    ):
        member_id = membership.organization_membership_id
        if member_id not in accounts:
            accounts[member_id] = {
                "organization_membership_id": member_id,
                "username": membership.organization_membership.user.get_username(),
                "memberships": [],
            }
        actual_department_id = membership.department_id
        if actual_department_id not in paths:
            paths[actual_department_id] = _full_path(actual_department_id, nodes)
        accounts[member_id]["memberships"].append({
            "id": membership.pk,
            "department_id": actual_department_id,
            "path": paths[actual_department_id],
            "is_direct": actual_department_id == department_id,
        })
    return list(accounts.values())


@transaction.atomic
def set_directory_policy(*, user, org_id, visibility):
    organization = _organization(user, org_id, lock=True)
    if user.pk != organization.owner_id:
        raise PermissionDenied
    if visibility not in organization.DepartmentDirectoryVisibility.values:
        raise DepartmentConflict("Выберите допустимую видимость каталога.")

    old_visibility = organization.department_directory_visibility
    if old_visibility != visibility:
        organization.department_directory_visibility = visibility
        organization.save(update_fields=["department_directory_visibility"])
        record(
            organization_id=org_id, actor_id=user.pk, action="organization.updated",
            object_type="organization", object_id=org_id,
            changes={"department_directory_visibility": {"old": old_visibility, "new": visibility}},
        )


def _department_invitations(department):
    return DepartmentInvitation.objects.filter(
        organization_id=department.organization_id, department=department,
    )


def managed_invitations(*, user, org_id, department_id):
    department = _managed_department(user, org_id, department_id, "departments.invitations.manage")
    return _department_invitations(department).order_by("-id")


def _resolve_invitation(invitation, status):
    invitation.status = status
    invitation.resolved_at = timezone.now()
    invitation.save(update_fields=["status", "resolved_at"])


@transaction.atomic
def issue_invitation(*, user, org_id, department_id, organization_membership_id):
    from apps.notifications.producers import emit

    department = _managed_department(user, org_id, department_id, "departments.invitations.manage", lock=True)
    organization = department.organization
    candidates = _active_org_members(organization)
    access = Access(user, organization)
    if (
        organization.department_directory_visibility != organization.DepartmentDirectoryVisibility.ORGANIZATION
        and not access.allows("departments.directory.read")
    ):
        _, _, branch_members, _ = _directory_context(user, org_id, department_id)
        candidates = candidates.filter(pk__in=branch_members.values("organization_membership_id"))
    target = get_object_or_404(
        candidates.select_for_update(of=("self",)),
        pk=organization_membership_id,
    )
    if department.memberships.filter(organization_membership=target, is_active=True).exists():
        raise DepartmentConflict("Пользователь уже состоит в этом подразделении.")
    invitations = _department_invitations(department).filter(organization_membership=target)
    if invitations.filter(status=DepartmentInvitation.Status.PENDING).exists():
        raise DepartmentConflict("Для этого пользователя уже есть действующее приглашение.")

    invitation = invitations.create(
        organization=department.organization, department=department,
        organization_membership=target, invited_by=user,
    )
    record(
        organization_id=org_id, actor_id=user.pk, action="department_invitation.issued",
        object_type="department_invitation", object_id=invitation.pk, department_id=department.pk,
        changes={"status": {"old": None, "new": invitation.status}},
        metadata={"organization_membership_id": target.pk},
    )
    emit(user, "department.invited", "department_invitation", invitation, [target.user_id],
         key=f"department.invite:{invitation.pk}")
    return invitation


@transaction.atomic
def revoke_invitation(*, user, org_id, department_id, invitation_id):
    department = _managed_department(user, org_id, department_id, "departments.invitations.manage", lock=True)
    invitation = get_object_or_404(
        _department_invitations(department),
        pk=invitation_id,
        organization_membership__is_active=True,
        organization_membership__user__is_active=True,
    )
    if invitation.status != DepartmentInvitation.Status.PENDING:
        raise DepartmentConflict("Приглашение уже закрыто.")

    _resolve_invitation(invitation, DepartmentInvitation.Status.REVOKED)
    record(
        organization_id=org_id, actor_id=user.pk, action="department_invitation.revoked",
        object_type="department_invitation", object_id=invitation.pk, department_id=department.pk,
        changes={"status": {"old": DepartmentInvitation.Status.PENDING, "new": invitation.status}},
        metadata={"organization_membership_id": invitation.organization_membership_id},
    )


def own_invitations(*, user):
    if not user.is_authenticated or not user.is_active:
        return DepartmentInvitation.objects.none()
    return DepartmentInvitation.objects.filter(
        organization_membership__user=user,
        organization_membership__is_active=True,
        organization_membership__user__is_active=True,
    ).order_by("-id")


@transaction.atomic
def respond_to_invitation(*, user, invitation_id, accept):
    locator = get_object_or_404(
        own_invitations(user=user).values("organization_id", "department_id"), pk=invitation_id,
    )
    organization = _organization(user, locator["organization_id"], lock=True)
    department = _department(organization, locator["department_id"], lock=True)
    get_object_or_404(
        _active_org_members(organization).select_for_update(of=("self",)), user=user,
    )
    invitation = get_object_or_404(
        own_invitations(user=user), pk=invitation_id, department=department, organization=organization,
    )
    if invitation.status != DepartmentInvitation.Status.PENDING:
        raise DepartmentConflict("Приглашение уже закрыто.")

    status = DepartmentInvitation.Status.DECLINED
    metadata = {"organization_membership_id": invitation.organization_membership_id}
    if accept:
        membership, created = DepartmentMembership.objects.select_for_update().get_or_create(
            organization=organization, department=department,
            organization_membership_id=invitation.organization_membership_id,
        )
        if not created:
            if membership.is_active:
                raise DepartmentConflict("Вы уже состоите в этом подразделении.")
            membership.is_active = True
            membership.ended_at = None
            membership.save(update_fields=["is_active", "ended_at"])
        status = DepartmentInvitation.Status.ACCEPTED
        metadata["membership_id"] = membership.pk

    _resolve_invitation(invitation, status)
    record(
        organization_id=organization.pk, actor_id=user.pk, action=f"department_invitation.{status}",
        object_type="department_invitation", object_id=invitation.pk, department_id=department.pk,
        changes={"status": {"old": DepartmentInvitation.Status.PENDING, "new": status}},
        metadata=metadata,
    )
    return invitation


@transaction.atomic
def end_membership(*, user, org_id, department_id, membership_id):
    department = _managed_department(
        user, org_id, department_id, "departments.members.manage", lock=True,
    )
    membership = get_object_or_404(
        department.memberships,
        pk=membership_id,
        organization_id=org_id,
        organization_membership__is_active=True,
        organization_membership__user__is_active=True,
    )
    if membership.is_active:
        membership.is_active = False
        membership.ended_at = timezone.now()
        membership.save(update_fields=["is_active", "ended_at"])
        record(
            organization_id=org_id, actor_id=user.pk, action="department_membership.removed",
            object_type="department_membership", object_id=membership.pk, department_id=department.pk,
            changes={"is_active": {"old": True, "new": False}},
            metadata={"organization_membership_id": membership.organization_membership_id},
        )
