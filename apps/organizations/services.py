from django.core.exceptions import PermissionDenied
from django.db import transaction

from .models import Organization, OrganizationMembership


def can_create_organization(user):
    return user.has_perm("organizations.add_organization")


@transaction.atomic
def create_organization(*, user, name):
    if not can_create_organization(user):
        raise PermissionDenied

    organization = Organization.objects.create(name=name, owner=user)
    OrganizationMembership.objects.create(organization=organization, user=user)
    return organization


def visible_organizations(user):
    if not user.is_authenticated or not user.is_active:
        return Organization.objects.none()

    return Organization.objects.filter(
        memberships__user=user,
        memberships__is_active=True,
    ).order_by("id")
