import hashlib
import unicodedata
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.audit.services import record

from .models import ORGANIZATION_NAME_MAX_LENGTH, Organization, OrganizationCreationReceipt, OrganizationMembership
from .platform import CreationConflict, CreationValidation, may_create, policy_row, valid_text


def can_create_organization(user):
    return may_create(user, policy_row().mode)


def clean_organization_name(name):
    name = name.strip()
    if not name or len(name) > ORGANIZATION_NAME_MAX_LENGTH or not valid_text(name):
        raise CreationValidation("Укажите допустимое название организации.")
    return name


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _name_digest(name):
    normalized = " ".join(unicodedata.normalize("NFC", name).casefold().split())
    return _digest(normalized)


def _create_organization_for_owner(*, user, name, actor):
    organization = Organization.objects.create(name=name, owner=user)
    OrganizationMembership.objects.create(organization=organization, user=user)
    record(
        organization_id=organization.pk, actor_id=actor.pk, action="organization.created",
        object_type="organization", object_id=organization.pk,
        changes={"name": {"old": None, "new": name}, "owner_id": {"old": None, "new": user.pk}},
    )
    return organization


@transaction.atomic
def create_organization(*, user, name, idempotency_key=None):
    policy = policy_row(lock=True)
    owner = get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).first()
    if owner is None or not may_create(owner, policy.mode):
        raise PermissionDenied
    name = clean_organization_name(name)
    fingerprint = _name_digest(name)
    if idempotency_key is not None:
        try:
            request_id = UUID(str(idempotency_key))
        except ValueError as exc:
            raise CreationValidation("Некорректный идентификатор запроса.") from exc
        key = _digest(f"request:{request_id}")
    else:
        key = _digest(f"name:{fingerprint}")

    receipt = OrganizationCreationReceipt.objects.select_related("organization").filter(user=owner, key=key).first()
    if receipt is not None:
        if receipt.name_digest != fingerprint:
            raise CreationConflict("Идентификатор уже использован для другого запроса.")
        if not receipt.organization.memberships.filter(user=owner, is_active=True).exists():
            raise CreationConflict("Результат предыдущего запроса больше недоступен.")
        return receipt.organization

    organization = _create_organization_for_owner(user=owner, name=name, actor=owner)
    OrganizationCreationReceipt.objects.create(user=owner, key=key, name_digest=fingerprint, organization=organization)
    return organization


def visible_organizations(user):
    if not user.is_authenticated or not user.is_active:
        return Organization.objects.none()

    return Organization.objects.filter(
        memberships__user=user,
        memberships__is_active=True,
    ).order_by("id")
