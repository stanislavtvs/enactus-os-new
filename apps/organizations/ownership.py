from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.audit.services import OWNERSHIP_TRANSFERRED, record
from apps.notifications.producers import emit

from .models import Organization, OwnershipTransfer

OFFER_LIFETIME = timedelta(days=7)


class OwnershipConflict(Exception):
    """The requested transfer conflicts with the current organization state."""


def _rows(organization=None):
    rows = OwnershipTransfer.objects.select_related(
        "organization", "from_owner__user", "recipient__user",
    )
    return rows if organization is None else rows.filter(organization=organization)


def _members(organization):
    return organization.memberships.filter(is_active=True, user__is_active=True)


def _owner_context(user, org_id, *, lock=False):
    organizations = Organization.objects.select_for_update() if lock else Organization.objects
    organization = get_object_or_404(organizations, pk=org_id)
    member = get_object_or_404(
        _members(organization).select_related("user"), user_id=user.pk,
    )
    if organization.owner_id != user.pk:
        raise PermissionDenied
    return organization, member


def _offer_status(offer, now):
    if offer.status != OwnershipTransfer.Status.PENDING:
        return offer.status
    if (
        offer.organization.owner_id != offer.from_owner.user_id
        or not offer.from_owner.is_active or not offer.from_owner.user.is_active
        or not offer.recipient.is_active or not offer.recipient.user.is_active
    ):
        return OwnershipTransfer.Status.REVOKED
    if offer.expires_at <= now:
        return OwnershipTransfer.Status.EXPIRED
    return offer.status


def _member_data(member):
    return {"id": member.pk, "username": member.user.get_username()}


def offer_data(offer, *, now=None):
    return {
        "id": offer.pk,
        "organization_id": offer.organization_id,
        "organization_name": offer.organization.name,
        "from_owner": _member_data(offer.from_owner),
        "recipient": _member_data(offer.recipient),
        "status": _offer_status(offer, now if now is not None else timezone.now()),
        "created_at": offer.created_at,
        "expires_at": offer.expires_at,
        "resolved_at": offer.resolved_at,
    }


def ownership_options(*, user, org_id):
    organization, owner = _owner_context(user, org_id)
    now = timezone.now()
    pending = _rows(organization).filter(
        status=OwnershipTransfer.Status.PENDING, expires_at__gt=now,
    ).first()
    candidates = _members(organization).exclude(pk=owner.pk).select_related("user").order_by("user__username", "pk")
    return {
        "owner": _member_data(owner),
        "pending": (
            offer_data(pending, now=now)
            if pending is not None and _offer_status(pending, now) == OwnershipTransfer.Status.PENDING
            else None
        ),
        "candidates": [_member_data(candidate) for candidate in candidates],
    }


def own_offers(*, user):
    rows = _rows().filter(
        recipient__user_id=user.pk, recipient__is_active=True, recipient__user__is_active=True,
    ).order_by("-created_at", "-pk")
    now = timezone.now()
    return [offer_data(offer, now=now) for offer in rows]


def _audit_offer(user, offer, verb):
    record(
        organization_id=offer.organization_id, actor_id=user.pk,
        action=f"ownership_transfer.{verb}", object_type="ownership_transfer", object_id=offer.pk,
        metadata={"membership_id": offer.recipient_id},
    )


def _notify(user, offer, verb, *recipient_ids):
    emit(
        user, f"organization.ownership_{verb}", "ownership_transfer", offer, recipient_ids,
        key=f"ownership-transfer:{offer.pk}:{verb}",
    )


def _resolve(offer, status, now):
    offer.status = status
    offer.resolved_at = now
    offer.save(update_fields=["status", "resolved_at"])


@transaction.atomic
def offer_ownership(*, user, org_id, recipient_id):
    organization, owner = _owner_context(user, org_id, lock=True)
    recipient = get_object_or_404(
        _members(organization).select_related("user").select_for_update(of=("self",)),
        pk=recipient_id,
    )
    if recipient.pk == owner.pk:
        raise OwnershipConflict("Нельзя передать владение самому себе.")

    now = timezone.now()
    pending = _rows(organization).select_for_update(of=("self",)).filter(
        status=OwnershipTransfer.Status.PENDING,
    ).first()
    if pending is not None:
        status = _offer_status(pending, now)
        if status == OwnershipTransfer.Status.PENDING:
            raise OwnershipConflict("В организации уже есть действующее предложение.")
        _resolve(pending, status, now)
        _audit_offer(user, pending, status)

    offer = OwnershipTransfer.objects.create(
        organization=organization, from_owner=owner, recipient=recipient, expires_at=now + OFFER_LIFETIME,
    )
    _audit_offer(user, offer, "offered")
    _notify(user, offer, "offered", recipient.user_id)
    return offer


def _require_pending(offer, now):
    if _offer_status(offer, now) != OwnershipTransfer.Status.PENDING:
        raise OwnershipConflict("Предложение больше не действует.")


@transaction.atomic
def revoke_offer(*, user, org_id, transfer_id):
    organization, _ = _owner_context(user, org_id, lock=True)
    offer = get_object_or_404(_rows(organization).select_for_update(of=("self",)), pk=transfer_id)
    now = timezone.now()
    _require_pending(offer, now)
    _resolve(offer, OwnershipTransfer.Status.REVOKED, now)
    _audit_offer(user, offer, "revoked")
    _notify(user, offer, "revoked", offer.from_owner.user_id, offer.recipient.user_id)
    return offer


@transaction.atomic
def respond_to_offer(*, user, transfer_id, accept):
    org_id = OwnershipTransfer.objects.filter(
        pk=transfer_id, recipient__user_id=user.pk,
    ).values_list("organization_id", flat=True).first()
    if org_id is None:
        raise Http404
    organization = get_object_or_404(Organization.objects.select_for_update(), pk=org_id)
    get_object_or_404(_members(organization).select_for_update(), user_id=user.pk)
    offer = get_object_or_404(
        _rows(organization).select_for_update(of=("self",)), pk=transfer_id, recipient__user_id=user.pk,
    )
    now = timezone.now()
    _require_pending(offer, now)
    if accept:
        previous_owner_id = organization.owner_id
        previous_owner = offer.from_owner
        changes = {"owner_id": {"old": previous_owner_id, "new": user.pk}}
        _resolve(offer, OwnershipTransfer.Status.ACCEPTED, now)
        organization.owner_id = user.pk
        organization.save(update_fields=["owner"])
        if previous_owner.can_manage_members:
            previous_owner.can_manage_members = False
            previous_owner.save(update_fields=["can_manage_members"])
            changes["can_manage_members"] = {"old": True, "new": False}
        record(
            organization_id=organization.pk, actor_id=user.pk,
            action=OWNERSHIP_TRANSFERRED, object_type="organization", object_id=organization.pk,
            changes=changes, metadata={"membership_id": offer.from_owner_id},
        )
        _notify(user, offer, "accepted", previous_owner_id, user.pk)
    else:
        _resolve(offer, OwnershipTransfer.Status.DECLINED, now)
        _audit_offer(user, offer, "declined")
        _notify(user, offer, "declined", offer.from_owner.user_id, offer.recipient.user_id)
    return offer
