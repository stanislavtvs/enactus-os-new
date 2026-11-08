import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.policy import Access

from .models import JoinRequest, Organization, OrganizationInvitation, OrganizationMembership

INVITATION_LIFETIME = timedelta(days=7)
RATE_WINDOW = timedelta(hours=1)
INVITATIONS_PER_HOUR = 20
REQUESTS_PER_HOUR = 5


class OnboardingError(Exception):
    """The requested change is invalid for the current organization or record state."""


class OnboardingRateLimit(OnboardingError):
    """The actor has reached the limit for new submissions in this organization."""


def _active_membership(user, organization):
    if not user.is_authenticated or not user.is_active:
        return None

    return OrganizationMembership.objects.filter(
        organization=organization,
        user=user,
        is_active=True,
    ).first()


def can_manage_members(user, organization):
    return Access(user, organization).allows("organizations.onboarding.manage")


def _locked_organization(org_id):
    return get_object_or_404(Organization.objects.select_for_update(), pk=org_id)


def _managed_organization(actor, org_id, *, owner_only=False):
    organization = _locked_organization(org_id)
    membership = _active_membership(actor, organization)
    if membership is None:
        raise Http404

    is_owner = actor.pk == organization.owner_id
    if not is_owner and (owner_only or not can_manage_members(actor, organization)):
        raise PermissionDenied
    return organization


def _activate_membership(organization, user_id):
    membership, created = OrganizationMembership.objects.select_for_update().get_or_create(
        organization=organization,
        user_id=user_id,
    )
    if not created and not membership.is_active:
        membership.is_active = True
        membership.can_manage_members = False
        membership.save(update_fields=["is_active", "can_manage_members"])


def _require_invitation_policy(organization):
    if organization.join_policy not in (Organization.JoinPolicy.INVITES, Organization.JoinPolicy.BOTH):
        raise OnboardingError("В этой организации вступление по приглашениям отключено.")


def _require_request_policy(organization):
    if organization.join_policy not in (Organization.JoinPolicy.REQUESTS, Organization.JoinPolicy.BOTH):
        raise OnboardingError("Эта организация не принимает заявки на вступление.")


@transaction.atomic
def set_join_policy(*, actor, org_id, policy):
    organization = _managed_organization(actor, org_id, owner_only=True)
    if policy not in Organization.JoinPolicy.values:
        raise OnboardingError("Выберите допустимый способ вступления.")

    organization.join_policy = policy
    organization.save(update_fields=["join_policy"])


@transaction.atomic
def set_member_manager(*, actor, org_id, membership_id, enabled):
    organization = _managed_organization(actor, org_id, owner_only=True)
    membership = get_object_or_404(
        OrganizationMembership.objects.select_for_update(),
        organization=organization,
        pk=membership_id,
    )
    if not membership.is_active:
        raise OnboardingError("Управление можно делегировать только активному участнику.")

    membership.can_manage_members = enabled
    membership.save(update_fields=["can_manage_members"])


def _token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def issue_invitation(*, actor, org_id, username):
    from apps.notifications.producers import emit

    organization = _managed_organization(actor, org_id)
    _require_invitation_policy(organization)
    recipient = get_user_model().objects.filter(username=username, is_active=True).first()
    if recipient is None:
        raise OnboardingError("Активный пользователь с таким именем не найден.")
    if _active_membership(recipient, organization) is not None:
        raise OnboardingError("Пользователь уже состоит в этой организации.")

    now = timezone.now()
    pending = OrganizationInvitation.objects.filter(
        organization=organization,
        recipient=recipient,
        status=OrganizationInvitation.Status.PENDING,
    )
    pending.filter(expires_at__lte=now).update(
        status=OrganizationInvitation.Status.EXPIRED,
        resolved_at=now,
    )
    if pending.exists():
        raise OnboardingError("Для этого пользователя уже есть действующее приглашение.")

    if organization.invitations.filter(
        invited_by=actor, created_at__gt=now - RATE_WINDOW,
    ).count() >= INVITATIONS_PER_HOUR:
        raise OnboardingRateLimit("Лимит приглашений исчерпан. Повторите попытку позже.")

    raw_token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation.objects.create(
        organization=organization,
        recipient=recipient,
        invited_by=actor,
        token_digest=_token_digest(raw_token),
        expires_at=now + INVITATION_LIFETIME,
    )
    emit(actor, "organization.invited", "organization_invitation", invitation, [recipient.pk],
         key=f"organization.invite:{invitation.pk}")
    return invitation, raw_token


def _resolve_invitation(invitation, status, now):
    invitation.status = status
    invitation.resolved_at = now
    invitation.save(update_fields=["status", "resolved_at"])


@transaction.atomic
def revoke_invitation(*, actor, org_id, invitation_id):
    organization = _managed_organization(actor, org_id)
    invitation = get_object_or_404(
        OrganizationInvitation, organization=organization, pk=invitation_id
    )
    if invitation.status != OrganizationInvitation.Status.PENDING:
        raise OnboardingError("Приглашение уже закрыто.")

    _resolve_invitation(invitation, OrganizationInvitation.Status.REVOKED, timezone.now())


@transaction.atomic
def respond_to_invitation(*, actor, invitation_id=None, token=None, accept):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    if (invitation_id is None) == (token is None):
        raise Http404

    invitations = OrganizationInvitation.objects.filter(recipient=actor)
    if token is not None:
        invitations = invitations.filter(token_digest=_token_digest(token))
    else:
        invitations = invitations.filter(pk=invitation_id)

    # Resolve the organization without a row lock so mutations always lock it first.
    locator = get_object_or_404(invitations.values("pk", "organization_id"))
    organization = _locked_organization(locator["organization_id"])
    invitation = get_object_or_404(invitations, pk=locator["pk"], organization=organization)

    now = timezone.now()
    if invitation.status != OrganizationInvitation.Status.PENDING:
        raise OnboardingError("Приглашение уже закрыто.")
    if invitation.expires_at <= now:
        raise OnboardingError("Срок действия приглашения истёк.")

    status = OrganizationInvitation.Status.DECLINED
    if accept:
        _require_invitation_policy(organization)
        _activate_membership(organization, actor.pk)
        status = OrganizationInvitation.Status.ACCEPTED

    _resolve_invitation(invitation, status, now)
    return invitation


@transaction.atomic
def submit_join_request(*, actor, org_id):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied

    organization = _locked_organization(org_id)
    _require_request_policy(organization)
    if _active_membership(actor, organization) is not None:
        raise OnboardingError("Вы уже состоите в этой организации.")
    if organization.join_requests.filter(applicant=actor, status=JoinRequest.Status.PENDING).exists():
        raise OnboardingError("Ваша заявка уже на рассмотрении.")
    if organization.join_requests.filter(
        applicant=actor, created_at__gt=timezone.now() - RATE_WINDOW,
    ).count() >= REQUESTS_PER_HOUR:
        raise OnboardingRateLimit("Лимит заявок исчерпан. Повторите попытку позже.")

    return JoinRequest.objects.create(organization=organization, applicant=actor)


@transaction.atomic
def review_join_request(*, actor, org_id, request_id, approve):
    organization = _managed_organization(actor, org_id)
    join_request = get_object_or_404(JoinRequest, organization=organization, pk=request_id)
    if join_request.status != JoinRequest.Status.PENDING:
        raise OnboardingError("Заявка уже рассмотрена.")

    status = JoinRequest.Status.REJECTED
    if approve:
        _require_request_policy(organization)
        if not join_request.applicant.is_active:
            raise OnboardingError("Учётная запись заявителя неактивна.")
        _activate_membership(organization, join_request.applicant_id)
        status = JoinRequest.Status.APPROVED

    join_request.status = status
    join_request.reviewed_by = actor
    join_request.reviewed_at = timezone.now()
    join_request.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return join_request
