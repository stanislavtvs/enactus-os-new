"""Domain entry points for durable internal notifications."""
from django.db.models import Q

from apps.access.policy import Access, LEGACY_MEMBER_MANAGER

from .services import publish


def emit(actor, kind, object_type, record, recipient_ids, *, key=None, data=None):
    return publish(
        organization_id=record.organization_id, actor_id=actor.pk if actor is not None else None,
        kind=kind, object_type=object_type, object_id=record.pk,
        recipient_ids=recipient_ids, key=key, data=data,
    )


def scoped_recipients(organization, permissions, allowed, *, membership_id=None):
    """Narrow to candidate grants, then apply the domain's existing live RBAC check."""
    candidate = Q(user_id=organization.owner_id) | Q(
        role_assignments__revoked_at__isnull=True,
        role_assignments__role__is_archived=False,
        role_assignments__role__permissions__code__in=permissions,
    )
    if LEGACY_MEMBER_MANAGER.intersection(permissions):
        candidate |= Q(can_manage_members=True)
    members = organization.memberships.filter(
        candidate, is_active=True, user__is_active=True,
    ).select_related("user").distinct()
    if membership_id is not None:
        members = members.filter(pk=membership_id)
    parents = dict(organization.departments.filter(is_archived=False).values_list("id", "parent_id"))
    result = set()
    for membership in members:
        access = Access(membership.user, organization)
        access.membership = membership
        access._parents = parents
        if allowed(access):
            result.add(membership.user_id)
    return result


def calendar_recipients(event):
    return event.invitations.filter(
        revoked_at__isnull=True, recipient__is_active=True, recipient__user__is_active=True,
    ).values_list("recipient__user_id", flat=True)
