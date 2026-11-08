from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.audit.services import record

from .models import Organization, OrganizationMembership


def can_create_organization(user):
    return user.has_perm("organizations.add_organization")


@transaction.atomic
def create_organization(*, user, name):
    if not can_create_organization(user):
        raise PermissionDenied

    organization = Organization.objects.create(name=name, owner=user)
    OrganizationMembership.objects.create(organization=organization, user=user)
    record(
        organization_id=organization.pk, actor_id=user.pk, action="organization.created",
        object_type="organization", object_id=organization.pk,
        changes={"name": {"old": None, "new": name}, "owner_id": {"old": None, "new": user.pk}},
    )
    return organization


def visible_organizations(user):
    if not user.is_authenticated or not user.is_active:
        return Organization.objects.none()

    return Organization.objects.filter(
        memberships__user=user,
        memberships__is_active=True,
    ).order_by("id")
