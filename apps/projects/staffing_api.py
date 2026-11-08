from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Count, Q
from ninja import PatchDict, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.policy import Access, Permission
from apps.access.services import organization_access

from . import staffing
from .models import StaffingAllocation, StaffingRequest
from .schemas import RecordId
from .services import visible_project
from .staffing_schemas import (
    AllocateInput,
    AssignmentModeInput,
    CandidateOutput,
    InvitationDecision,
    InvitationOutput,
    RejectInput,
    RequestInput,
    RequestOutput,
    TeamOutput,
)

router = Router(auth=django_auth)


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except staffing.StaffingConflict as exc:
        raise HttpError(409, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in {
            "unique_project_membership", "unique_pending_project_staffing_allocation",
            "staffing_allocation_requires_active_source", "staffing_allocation_source_branch",
            "project_membership_requires_active_member", "staffing_requires_live_project",
            "project_requires_active_department",
        }:
            raise
        raise HttpError(409, "Состояние членства или распределения изменилось.") from None


def _allocation_data(item, can_revoke):
    return {
        "id": item.pk,
        "username": item.organization_membership.user.get_username(),
        "department_name": item.department_membership.department.name,
        "status": item.status,
        "mode": item.mode,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
        "allocated_by": item.allocated_by.get_username() if item.allocated_by else None,
        "can_revoke": can_revoke and item.status == StaffingAllocation.Status.PENDING,
    }


def _request_data(user, item, *, include_history=True, access=None):
    access = access if access is not None else Access(user, item.organization)
    can_request = staffing.can_request_staffing(access, item.project)
    can_allocate = staffing.can_allocate_request(access, item)
    history = []
    if include_history:
        allocations = item.allocations.select_related(
            "department_membership__department", "organization_membership__user", "allocated_by",
        ).order_by("id")
        for allocation in allocations:
            source_allowed = access.allows(
                Permission.DEPARTMENTS_STAFFING_ALLOCATE, allocation.department_membership.department,
            )
            if can_request or source_allowed:
                history.append(_allocation_data(allocation, can_allocate and source_allowed))
    if not hasattr(item, "accepted_count"):
        counts = item.allocations.aggregate(
            accepted_count=Count("id", filter=Q(status=StaffingAllocation.Status.ACCEPTED)),
            pending_count=Count("id", filter=Q(status=StaffingAllocation.Status.PENDING)),
        )
    else:
        counts = {"accepted_count": item.accepted_count, "pending_count": item.pending_count}
    open_request = item.status in (StaffingRequest.Status.SENT, StaffingRequest.Status.PARTIAL)
    return {
        "id": item.pk,
        "project_id": item.project_id,
        "project_name": item.project.name,
        "department_id": item.department_id,
        "department_name": item.department.name,
        "requested_count": item.requested_count,
        "description": item.description,
        "specialization": item.specialization,
        "desired_start": item.desired_start,
        "duration_days": item.duration_days,
        "status": item.status,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "submitted_at": item.submitted_at,
        "handled_by": item.handled_by.get_username() if item.handled_by else None,
        "handled_at": item.handled_at,
        "comment": item.comment,
        **counts,
        "can_edit": can_request and not item.project.is_archived and (
            open_request or item.status == StaffingRequest.Status.DRAFT
        ),
        "can_cancel": can_request and not item.project.is_archived and (
            open_request or item.status == StaffingRequest.Status.DRAFT
        ),
        "can_allocate": can_allocate and open_request and not item.project.is_archived,
        "awaiting_handler": open_request and item.handled_by_id is None,
        "history": history,
    }


def _invitation_data(item):
    return {
        "id": item.pk,
        "organization_name": item.organization.name,
        "project_name": item.project.name,
        "department_name": item.department_membership.department.name,
        "status": item.status,
        "created_at": item.created_at,
    }


@router.get("/{org_id}/projects/{project_id}/team/", response=TeamOutput, url_name="project_team_api")
def project_team(request, org_id: RecordId, project_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    project = visible_project(access=access, project_id=project_id)
    can_request = staffing.can_request_staffing(access, project)
    can_view_team = staffing.can_view_team(access, project)
    members = (
        _perform(staffing.get_team, user=request.user, org_id=org_id, project_id=project_id)
        if can_view_team or not can_request else []
    )
    lead = project.lead_membership
    return {
        "lead": {"id": lead.pk, "username": lead.user.get_username()} if lead else None,
        "members": [
            {
                "id": member.pk,
                "username": member.organization_membership.user.get_username(),
                "source_department_name": member.source_department.name if member.source_department else None,
                "is_active": member.is_active,
                "joined_at": member.joined_at,
                "ended_at": member.ended_at,
            } for member in members
        ],
        "can_manage_team": not project.is_archived and staffing.can_manage_team(access, project),
        "can_view_team": can_view_team,
        "can_view_requests": can_request,
        "can_request": not project.is_archived and can_request,
    }


@router.delete("/{org_id}/projects/{project_id}/team/{membership_id}/", response={204: None})
def end_project_membership(request, org_id: RecordId, project_id: RecordId, membership_id: RecordId):
    _perform(staffing.end_membership, user=request.user, org_id=org_id,
             project_id=project_id, membership_id=membership_id)
    return 204, None


@router.get("/{org_id}/projects/{project_id}/staffing-requests/", response=list[RequestOutput])
def project_requests(request, org_id: RecordId, project_id: RecordId):
    items = _perform(staffing.list_project_requests, user=request.user, org_id=org_id, project_id=project_id)
    return _request_list(request.user, items)


@router.post("/{org_id}/projects/{project_id}/staffing-requests/", response={201: RequestOutput})
def create_staffing_request(request, org_id: RecordId, project_id: RecordId, payload: RequestInput):
    item = _perform(staffing.create_request, user=request.user, org_id=org_id,
                    project_id=project_id, **payload.model_dump())
    return 201, _request_data(request.user, item)


@router.get("/{org_id}/staffing-requests/", response=list[RequestOutput], url_name="staffing_request_list")
def department_requests(request, org_id: RecordId, department_id: RecordId | None = None):
    items = _perform(staffing.list_department_requests, user=request.user, org_id=org_id, department_id=department_id)
    return _request_list(request.user, items)


def _request_list(user, items):
    access = None
    result = []
    for item in items:
        if access is None:
            access = Access(user, item.organization)
        result.append(_request_data(user, item, include_history=False, access=access))
    return result


@router.get("/{org_id}/staffing-requests/{request_id}/", response=RequestOutput)
def get_staffing_request(request, org_id: RecordId, request_id: RecordId):
    item = _perform(staffing.get_request, user=request.user, org_id=org_id, request_id=request_id)
    return _request_data(request.user, item)


def _change(request, operation, org_id, request_id, **kwargs):
    item = _perform(operation, user=request.user, org_id=org_id, request_id=request_id, **kwargs)
    return _request_data(request.user, item)


@router.patch("/{org_id}/staffing-requests/{request_id}/", response=RequestOutput)
def update_staffing_request(request, org_id: RecordId, request_id: RecordId, payload: PatchDict[RequestInput]):
    return _change(request, staffing.update_request, org_id, request_id, changes=payload)


@router.delete("/{org_id}/staffing-requests/{request_id}/", response=RequestOutput)
def cancel_staffing_request(request, org_id: RecordId, request_id: RecordId):
    return _change(request, staffing.cancel_request, org_id, request_id)


@router.post("/{org_id}/staffing-requests/{request_id}/submit/", response=RequestOutput)
def submit_staffing_request(request, org_id: RecordId, request_id: RecordId):
    return _change(request, staffing.submit_request, org_id, request_id)


@router.post("/{org_id}/staffing-requests/{request_id}/allocate/", response=RequestOutput)
def allocate_staffing_request(request, org_id: RecordId, request_id: RecordId, payload: AllocateInput):
    return _change(request, staffing.allocate, org_id, request_id,
                   department_membership_ids=payload.department_membership_ids)


@router.post("/{org_id}/staffing-requests/{request_id}/reject/", response=RequestOutput)
def reject_staffing_request(request, org_id: RecordId, request_id: RecordId, payload: RejectInput):
    return _change(request, staffing.reject_request, org_id, request_id, comment=payload.comment)


@router.get("/{org_id}/staffing-requests/{request_id}/candidates/", response=list[CandidateOutput])
def staffing_candidates(request, org_id: RecordId, request_id: RecordId):
    candidates = _perform(staffing.list_candidates, user=request.user, org_id=org_id, request_id=request_id)
    return [{
        "id": candidate["department_membership_id"],
        "username": candidate["username"],
        "department_name": candidate["department_name"],
        "projects": [project["name"] for project in candidate["visible_projects"]],
    } for candidate in candidates]


@router.delete("/{org_id}/staffing-requests/{request_id}/allocations/{allocation_id}/", response=RequestOutput)
def revoke_project_invitation(request, org_id: RecordId, request_id: RecordId, allocation_id: RecordId):
    return _change(request, staffing.revoke_invitation, org_id, request_id, allocation_id=allocation_id)


@router.get("/project-invitations/", response=list[InvitationOutput], url_name="project_invitation_inbox")
def project_invitations(request):
    return [_invitation_data(item) for item in staffing.own_invitations(user=request.user)]


@router.post("/project-invitations/{allocation_id}/respond/", response=InvitationOutput)
def respond_to_project_invitation(request, allocation_id: RecordId, payload: InvitationDecision):
    item = _perform(staffing.respond_to_invitation, user=request.user, allocation_id=allocation_id,
                    accept=payload.decision == "accept")
    return _invitation_data(item)


@router.patch("/{org_id}/staffing-policy/", response={204: None})
def change_staffing_policy(request, org_id: RecordId, payload: AssignmentModeInput):
    _perform(staffing.set_assignment_mode, user=request.user, org_id=org_id, mode=payload.mode)
    return 204, None
