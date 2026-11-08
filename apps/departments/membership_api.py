from datetime import datetime
from typing import Annotated, Literal

from django.db import IntegrityError
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import ConfigDict, Field

from apps.organizations.models import Organization

from . import membership_services as services
from .api import RecordId, _perform

router = Router(auth=django_auth)
ENGAGEMENT_CONFLICT_CONSTRAINTS = {
    "department_engagement_requires_active_parents",
    "unique_department_membership",
    "unique_pending_department_invitation",
}


class DirectoryPolicyInput(Schema):
    model_config = ConfigDict(extra="forbid")
    visibility: Organization.DepartmentDirectoryVisibility


class DirectoryMembershipOutput(Schema):
    id: int
    department_id: int
    path: list[str]
    is_direct: bool


class DirectoryMemberOutput(Schema):
    organization_membership_id: int
    username: str
    memberships: list[DirectoryMembershipOutput]


class InvitationInput(Schema):
    model_config = ConfigDict(extra="forbid")
    organization_membership_id: Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class InvitationResponseInput(Schema):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "decline"]


class InvitationOutput(Schema):
    id: int
    organization_id: int
    organization_name: str
    department_id: int
    department_name: str
    organization_membership_id: int
    username: str
    status: str
    created_at: datetime

    @staticmethod
    def resolve_organization_name(invitation):
        return invitation.organization.name

    @staticmethod
    def resolve_department_name(invitation):
        return invitation.department.name

    @staticmethod
    def resolve_username(invitation):
        return invitation.organization_membership.user.get_username()


def _with_invitation_details(invitations):
    return invitations.select_related("organization", "department", "organization_membership__user")


def _perform_engagement_change(operation, **kwargs):
    try:
        return _perform(operation, **kwargs)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in ENGAGEMENT_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Состояние подразделения или членства изменилось.") from None


@router.get(
    "/{org_id}/departments/{department_id}/members/",
    response=list[DirectoryMemberOutput],
    url_name="department_member_list",
)
def list_members(
    request,
    org_id: RecordId,
    department_id: RecordId,
    scope: Literal["branch", "direct", "others"] = "branch",
):
    return services.department_directory(
        user=request.user, org_id=org_id, department_id=department_id, scope=scope,
    )


@router.patch("/{org_id}/department-directory-policy/", response={204: None})
def update_directory_policy(request, org_id: RecordId, payload: DirectoryPolicyInput):
    _perform(
        services.set_directory_policy, user=request.user, org_id=org_id, visibility=payload.visibility,
    )
    return 204, None


@router.delete("/{org_id}/departments/{department_id}/members/{membership_id}/", response={204: None})
def end_membership(request, org_id: RecordId, department_id: RecordId, membership_id: RecordId):
    _perform(
        services.end_membership,
        user=request.user, org_id=org_id, department_id=department_id, membership_id=membership_id,
    )
    return 204, None


@router.get("/{org_id}/departments/{department_id}/invitations/", response=list[InvitationOutput])
def list_department_invitations(request, org_id: RecordId, department_id: RecordId):
    invitations = _perform(
        services.managed_invitations, user=request.user, org_id=org_id, department_id=department_id,
    )
    return _with_invitation_details(invitations)


@router.post("/{org_id}/departments/{department_id}/invitations/", response={201: InvitationOutput})
def create_department_invitation(request, org_id: RecordId, department_id: RecordId, payload: InvitationInput):
    invitation = _perform_engagement_change(
        services.issue_invitation,
        user=request.user, org_id=org_id, department_id=department_id,
        organization_membership_id=payload.organization_membership_id,
    )
    return 201, invitation


@router.delete(
    "/{org_id}/departments/{department_id}/invitations/{invitation_id}/",
    response={204: None},
)
def revoke_department_invitation(request, org_id: RecordId, department_id: RecordId, invitation_id: RecordId):
    _perform(
        services.revoke_invitation,
        user=request.user, org_id=org_id, department_id=department_id, invitation_id=invitation_id,
    )
    return 204, None


@router.get(
    "/department-invitations/",
    response=list[InvitationOutput],
    url_name="department_invitation_inbox",
)
def department_invitation_inbox(request):
    return _with_invitation_details(services.own_invitations(user=request.user))


@router.post("/department-invitations/{invitation_id}/respond/", response=InvitationOutput)
def respond_to_department_invitation(request, invitation_id: RecordId, payload: InvitationResponseInput):
    return _perform_engagement_change(
        services.respond_to_invitation,
        user=request.user, invitation_id=invitation_id, accept=payload.decision == "accept",
    )
