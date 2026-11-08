from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.platform import record_platform
from apps.audit.services import record

from .models import Organization, OwnershipTransfer
from .platform_access import is_platform_admin

SCENARIOS = ("reactivate", "transfer")
REASON_MAX_LENGTH = 250


class OwnershipRecoveryError(Exception):
    """An operator-supplied recovery request failed its safety checks."""


@dataclass(frozen=True)
class RecoveryResult:
    organization_id: int
    scenario: str
    old_owner_id: int
    new_owner_id: int
    old_owner_active: bool
    applied: bool


def _clean_reason(reason):
    reason = reason.strip()
    if not reason or len(reason) > REASON_MAX_LENGTH or not reason.isprintable():
        raise OwnershipRecoveryError("Specify a printable incident ticket or reason (up to 250 characters).")
    return reason


def _pending_offers_for(user_id):
    return OwnershipTransfer.objects.filter(status=OwnershipTransfer.Status.PENDING).filter(
        Q(from_owner__user_id=user_id) | Q(recipient__user_id=user_id),
    )


def _lock_organizations(organization_id, scenario):
    rows = Organization.objects.select_for_update()
    if scenario == "transfer":
        return rows.filter(pk=organization_id).first(), set()

    observed_owner_id = Organization.objects.filter(pk=organization_id).values_list("owner_id", flat=True).first()
    if observed_owner_id is None:
        return None, set()
    affected = _pending_offers_for(observed_owner_id).values("organization_id")
    locked = {
        organization.pk: organization
        for organization in rows.filter(Q(pk=organization_id) | Q(pk__in=affected)).order_by("pk")
    }
    organization = locked.get(organization_id)
    if organization is not None and organization.owner_id != observed_owner_id:
        raise OwnershipRecoveryError("The expected owner does not match the current owner.")
    return organization, set(locked)


@transaction.atomic
def recover_organization_owner(
    *, organization_id, expected_owner, scenario, operator, approver, reason,
    candidate=None, dry_run=False, confirm=False,
):
    if dry_run == confirm:
        raise OwnershipRecoveryError("Choose exactly one of --dry-run or --confirm.")
    if scenario not in SCENARIOS:
        raise OwnershipRecoveryError("Choose a supported recovery scenario.")
    if bool(candidate) != (scenario == "transfer"):
        raise OwnershipRecoveryError("Specify --candidate only for the transfer scenario.")
    reason = _clean_reason(reason)
    if not 0 < organization_id < 2**63:
        raise OwnershipRecoveryError("Invalid organization ID.")

    organization, locked_org_ids = _lock_organizations(organization_id, scenario)
    if organization is None:
        raise OwnershipRecoveryError("Organization not found.")
    names = {operator, approver, expected_owner}
    if candidate is not None:
        names.add(candidate)
    users = {
        user.get_username(): user
        for user in get_user_model().objects.select_for_update().filter(
            Q(pk=organization.owner_id) | Q(username__in=names),
        ).order_by("pk")
    }
    operator_account = users.get(operator)
    approver_account = users.get(approver)
    if (
        operator_account is None or approver_account is None
        or operator_account.pk == approver_account.pk
        or not is_platform_admin(operator_account) or not is_platform_admin(approver_account)
    ):
        raise OwnershipRecoveryError("Operator and approver must be distinct active platform administrators.")

    old_owner = users.get(expected_owner)
    if old_owner is None or old_owner.pk != organization.owner_id:
        raise OwnershipRecoveryError("The expected owner does not match the current owner.")
    new_owner = old_owner if scenario == "reactivate" else users.get(candidate)
    if new_owner is None or (scenario == "transfer" and new_owner.pk == old_owner.pk):
        raise OwnershipRecoveryError("The candidate must be another active participant of this organization.")
    affected_users = {old_owner.pk, new_owner.pk}
    if affected_users.intersection({operator_account.pk, approver_account.pk}):
        raise OwnershipRecoveryError("Neither operator nor approver may be an affected account.")

    memberships = {
        membership.user_id: membership
        for membership in organization.memberships.select_for_update().filter(
            user_id__in=affected_users,
        ).order_by("pk")
    }
    old_membership = memberships.get(old_owner.pk)
    new_membership = memberships.get(new_owner.pk)
    if scenario == "reactivate":
        if old_owner.is_active or old_owner.registration_pending or old_membership is None or not old_membership.is_active:
            raise OwnershipRecoveryError(
                "Reactivation requires an inactive owner with an active membership and no pending registration."
            )
    elif not new_owner.is_active or new_membership is None or not new_membership.is_active:
        raise OwnershipRecoveryError("The candidate must be another active participant of this organization.")

    result = RecoveryResult(
        organization_id=organization.pk, scenario=scenario, old_owner_id=old_owner.pk,
        new_owner_id=new_owner.pk, old_owner_active=old_owner.is_active, applied=confirm,
    )
    if dry_run:
        return result

    if scenario == "reactivate":
        old_owner.is_active = True
        old_owner.save(update_fields=["is_active"])
        # Account activation is global, so invalidate stale offers in every locked organization.
        revoked_offers = _pending_offers_for(old_owner.pk).filter(organization_id__in=locked_org_ids).update(
            status=OwnershipTransfer.Status.REVOKED, resolved_at=timezone.now(),
        )
        changes = {"is_active": {"old": False, "new": True}}
    else:
        revoked_offers = organization.ownership_transfers.filter(status=OwnershipTransfer.Status.PENDING).count()
        organization.owner = new_owner
        organization.save(update_fields=["owner"])
        changes = {"owner_id": {"old": old_owner.pk, "new": new_owner.pk}}
        if old_membership is not None and old_membership.can_manage_members:
            old_membership.can_manage_members = False
            old_membership.save(update_fields=["can_manage_members"])
            changes["can_manage_members"] = {"old": True, "new": False}

    record_platform(
        actor=operator_account, action="platform.ownership_recovered", subject_id=organization.pk,
        metadata={
            "organization_id": organization.pk, "scenario": scenario, "reason": reason,
            "approver_id": approver_account.pk, "old_owner_id": old_owner.pk, "new_owner_id": new_owner.pk,
            "revoked_offers": revoked_offers,
        },
    )
    record(
        organization_id=organization.pk, actor_id=operator_account.pk,
        action="organization.ownership_recovered", object_type="organization", object_id=organization.pk,
        changes=changes,
    )
    return result
