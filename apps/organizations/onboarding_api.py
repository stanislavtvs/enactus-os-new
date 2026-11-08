from datetime import datetime
from typing import Annotated, Literal

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import Field, StringConstraints

from . import onboarding
from .join_links import make_join_code, read_join_code
from .models import Organization, OrganizationInvitation
from .services import visible_organizations

router = Router(auth=django_auth)
REQUEST_POLICIES = (Organization.JoinPolicy.REQUESTS, Organization.JoinPolicy.BOTH)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
InvitationToken = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]


class PolicyInput(Schema):
    policy: Organization.JoinPolicy


class InviteInput(Schema):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150, pattern=r"^[^\x00]+$")]


class ManagerInput(Schema):
    enabled: bool


class DecisionInput(Schema):
    approve: bool


class InvitationResponseInput(Schema):
    invitation_id: RecordId | None = None
    token: InvitationToken | None = None
    decision: Literal["accept", "decline"]


class MemberOutput(Schema):
    id: int
    username: str
    can_manage_members: bool


class InviteOutput(Schema):
    id: int
    organization_id: int
    status: str
    expires_at: datetime


class IssuedInviteOutput(InviteOutput):
    token: str


class ManagedInviteOutput(InviteOutput):
    recipient: str


class InboxInviteOutput(InviteOutput):
    organization_name: str


class JoinOutput(Schema):
    id: int
    organization_id: int
    status: str


class ManagedJoinOutput(JoinOutput):
    applicant: str


class ManageOutput(Schema):
    join_policy: str
    join_code: str | None
    members: list[MemberOutput]
    invitations: list[ManagedInviteOutput]
    join_requests: list[ManagedJoinOutput]


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except onboarding.OnboardingRateLimit as exc:
        raise HttpError(429, str(exc)) from None
    except onboarding.OnboardingError as exc:
        raise HttpError(409, str(exc)) from None


def _manage_read(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    if not onboarding.can_manage_members(request.user, organization):
        raise HttpError(403, "Forbidden")
    return organization


def _invite_data(invitation):
    status = invitation.status
    if status == OrganizationInvitation.Status.PENDING and invitation.expires_at <= timezone.now():
        status = OrganizationInvitation.Status.EXPIRED
    return {
        "id": invitation.pk,
        "organization_id": invitation.organization_id,
        "status": status,
        "expires_at": invitation.expires_at,
    }


@router.get("/{org_id}/members/", response=ManageOutput)
def manage_members(request, org_id: RecordId):
    organization = _manage_read(request, org_id)
    return {
        "join_policy": organization.join_policy,
        "join_code": make_join_code(org_id) if organization.join_policy in REQUEST_POLICIES else None,
        "members": [
            {"id": member.pk, "username": member.user.get_username(),
             "can_manage_members": member.can_manage_members}
            for member in organization.memberships.filter(is_active=True).select_related("user").order_by("id")
        ],
        "invitations": [
            {**_invite_data(invitation), "recipient": invitation.recipient.get_username()}
            for invitation in organization.invitations.select_related("recipient").order_by("-id")
        ],
        "join_requests": [
            {"id": item.pk, "organization_id": org_id, "status": item.status,
             "applicant": item.applicant.get_username()}
            for item in organization.join_requests.select_related("applicant").order_by("-id")
        ],
    }


@router.patch("/{org_id}/join-policy/", response={204: None})
def update_join_policy(request, org_id: RecordId, payload: PolicyInput):
    _perform(onboarding.set_join_policy, actor=request.user, org_id=org_id, policy=payload.policy)
    return 204, None


@router.patch("/{org_id}/members/{membership_id}/", response={204: None})
def update_member_manager(request, org_id: RecordId, membership_id: RecordId, payload: ManagerInput):
    _perform(
        onboarding.set_member_manager, actor=request.user, org_id=org_id,
        membership_id=membership_id, enabled=payload.enabled,
    )
    return 204, None


@router.post("/{org_id}/invitations/", response={201: IssuedInviteOutput})
def create_invitation(request, response: HttpResponse, org_id: RecordId, payload: InviteInput):
    invitation, token = _perform(
        onboarding.issue_invitation, actor=request.user, org_id=org_id, username=payload.username,
    )
    response["Cache-Control"] = "no-store"
    return 201, {**_invite_data(invitation), "token": token}


@router.delete("/{org_id}/invitations/{invitation_id}/", response={204: None})
def revoke_invitation(request, org_id: RecordId, invitation_id: RecordId):
    _perform(
        onboarding.revoke_invitation, actor=request.user, org_id=org_id, invitation_id=invitation_id,
    )
    return 204, None


@router.get("/invitations/", response=list[InboxInviteOutput])
def invitation_inbox(request):
    invitations = OrganizationInvitation.objects.filter(
        recipient=request.user, status=OrganizationInvitation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).select_related("organization").order_by("-id")
    return [
        {**_invite_data(invitation), "organization_name": invitation.organization.name}
        for invitation in invitations
    ]


@router.post("/invitations/respond/", response=InviteOutput)
def respond_to_invitation(request, payload: InvitationResponseInput):
    if (payload.invitation_id is None) == (payload.token is None):
        raise HttpError(422, "Укажите одно приглашение.")
    invitation = _perform(
        onboarding.respond_to_invitation, actor=request.user, invitation_id=payload.invitation_id,
        token=payload.token, accept=payload.decision == "accept",
    )
    return _invite_data(invitation)


@router.post("/join/{code}/", response={201: JoinOutput})
def submit_join_request(request, code: str):
    organization = get_object_or_404(
        Organization, pk=read_join_code(code), join_policy__in=REQUEST_POLICIES,
    )
    return 201, _perform(onboarding.submit_join_request, actor=request.user, org_id=organization.pk)


@router.post("/{org_id}/join-requests/{request_id}/review/", response=JoinOutput)
def review_join_request(request, org_id: RecordId, request_id: RecordId, payload: DecisionInput):
    return _perform(
        onboarding.review_join_request, actor=request.user, org_id=org_id,
        request_id=request_id, approve=payload.approve,
    )
