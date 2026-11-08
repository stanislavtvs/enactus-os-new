from collections import defaultdict

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access
from apps.departments.models import DepartmentMembership
from apps.organizations.models import Organization

from .models import ProjectMembership, StaffingAllocation, StaffingRequest
from .services import _can_view, _context, visible_projects

REQUEST_PERMISSION = "projects.staffing.request"
ALLOCATE_PERMISSION = "departments.staffing.allocate"
TEAM_VIEW_PERMISSION = "projects.team.view"
TEAM_MANAGE_PERMISSION = "projects.team.manage"
OPEN_STATES = (StaffingRequest.Status.SENT, StaffingRequest.Status.PARTIAL)


class StaffingConflict(Exception):
    """A staffing operation conflicts with the current request or membership state."""


def _project(access, project_id, *, lock=False):
    projects = access.organization.projects.select_related("responsible_department")
    if lock:
        projects = projects.select_for_update(of=("self",))
    return get_object_or_404(projects, pk=project_id)


def _request_rows(organization):
    return organization.staffing_requests.select_related(
        "project__responsible_department", "department", "created_by", "handled_by",
    ).annotate(
        accepted_count=Count(
            "allocations", filter=Q(allocations__status=StaffingAllocation.Status.ACCEPTED),
        ),
        pending_count=Count(
            "allocations", filter=Q(allocations__status=StaffingAllocation.Status.PENDING),
        ),
    ).order_by("id")


def _request_result(request):
    return _request_rows(request.organization).get(pk=request.pk)


def _request_record(access, request_id, *, lock=False):
    requests = access.organization.staffing_requests.select_related(
        "project__responsible_department", "department",
    )
    if lock:
        requests = requests.select_for_update(of=("self", "project"))
    return get_object_or_404(requests, pk=request_id)


def _active_project(project):
    if project.is_archived:
        raise StaffingConflict("Архивный проект нельзя изменить.")


def can_request_staffing(access, project):
    return project.organization_id == access.organization.pk and access.membership is not None and (
        access.is_owner
        or (
            project.lead_membership_id == access.membership.pk
            and access.allows(REQUEST_PERMISSION, _context(project))
            and _can_view(access, project)
        )
    )


def _department_side(access, request):
    return (
        request.submitted_at is not None
        and not request.department.is_archived
        and access.allows(ALLOCATE_PERMISSION, request.department)
    )


def can_allocate_request(access, request):
    return _department_side(access, request) and (
        not request.project.is_archived and request.status in OPEN_STATES
    )


def can_open_staffing(access):
    return any(
        access.allows(ALLOCATE_PERMISSION, department)
        for department in access.organization.departments.filter(is_archived=False)
    )


def _require_project_side(access, project):
    if not _can_view(access, project):
        raise Http404
    if not can_request_staffing(access, project):
        raise PermissionDenied


def _require_department_side(access, request):
    if not _department_side(access, request):
        if can_request_staffing(access, request.project):
            raise PermissionDenied
        raise Http404


def _open_request(request):
    if request.status not in OPEN_STATES:
        raise StaffingConflict("Запрос уже закрыт или ещё не отправлен.")


def _active_department(access, department_id):
    department = get_object_or_404(access.organization.departments, pk=department_id)
    if department.is_archived:
        raise StaffingConflict("Архивное подразделение недоступно для запроса.")
    return department


def can_view_team(access, project):
    if not _can_view(access, project):
        return False
    return access.allows(TEAM_VIEW_PERMISSION, _context(project)) or (
        access.membership is not None and project.memberships.filter(
            organization=access.organization, is_active=True,
            organization_membership=access.membership,
        ).exists()
    )


def can_manage_team(access, project):
    return (
        not project.is_archived and _can_view(access, project)
        and access.allows(TEAM_MANAGE_PERMISSION, _context(project))
    )


def get_team(*, user, org_id, project_id):
    access = organization_access(user=user, org_id=org_id)
    project = _project(access, project_id)
    if not can_view_team(access, project):
        if not _can_view(access, project):
            raise Http404
        raise PermissionDenied
    return project.memberships.filter(organization=access.organization).select_related(
        "organization_membership__user", "source_department",
    ).order_by("id")


def list_project_requests(*, user, org_id, project_id):
    access = organization_access(user=user, org_id=org_id)
    project = _project(access, project_id)
    _require_project_side(access, project)
    return _request_rows(access.organization).filter(project=project)


def list_department_requests(*, user, org_id, department_id=None):
    access = organization_access(user=user, org_id=org_id)
    departments = access.organization.departments.filter(is_archived=False)
    if department_id is not None:
        department = get_object_or_404(departments, pk=department_id)
        if not access.allows(ALLOCATE_PERMISSION, department):
            raise PermissionDenied
        departments = [department]
    ids = [department.pk for department in departments if access.allows(ALLOCATE_PERMISSION, department)]
    return _request_rows(access.organization).filter(
        department_id__in=ids, submitted_at__isnull=False,
    )


def get_request(*, user, org_id, request_id):
    access = organization_access(user=user, org_id=org_id)
    request = get_object_or_404(_request_rows(access.organization), pk=request_id)
    if not (can_request_staffing(access, request.project) or _department_side(access, request)):
        raise Http404
    return request


@transaction.atomic
def create_request(*, user, org_id, project_id, department_id, **fields):
    access = organization_access(user=user, org_id=org_id, lock=True)
    project = _project(access, project_id, lock=True)
    _require_project_side(access, project)
    _active_project(project)
    department = _active_department(access, department_id)
    request = access.organization.staffing_requests.create(
        project=project, department=department, created_by=user, **fields,
    )
    return _request_result(request)


def _project_request_to_change(access, request_id):
    request = _request_record(access, request_id, lock=True)
    _require_project_side(access, request.project)
    _active_project(request.project)
    return request


@transaction.atomic
def update_request(*, user, org_id, request_id, changes):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _project_request_to_change(access, request_id)
    draft = request.status == StaffingRequest.Status.DRAFT
    if not draft:
        _open_request(request)
    fields = []
    if "department_id" in changes:
        if draft:
            request.department = _active_department(access, changes["department_id"])
            fields.append("department")
        elif changes["department_id"] != request.department_id:
            raise StaffingConflict("Нельзя сменить подразделение после отправки запроса.")
    if not draft and "requested_count" in changes:
        current = _request_result(request)
        count = changes["requested_count"]
        if count < current.accepted_count + current.pending_count:
            raise StaffingConflict("Количество мест меньше уже занятых или зарезервированных.")
        request.status = _allocation_status(current.accepted_count, count)
        fields.append("status")
    for field in ("requested_count", "description", "specialization", "desired_start", "duration_days"):
        if field in changes:
            setattr(request, field, changes[field])
            fields.append(field)
    if fields:
        request.save(update_fields=[*fields, "updated_at"])
    return _request_result(request)


@transaction.atomic
def submit_request(*, user, org_id, request_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _project_request_to_change(access, request_id)
    if request.status != StaffingRequest.Status.DRAFT:
        raise StaffingConflict("Отправить можно только черновик запроса.")
    _active_department(access, request.department_id)
    request.status = StaffingRequest.Status.SENT
    request.submitted_at = timezone.now()
    request.save(update_fields=["status", "submitted_at", "updated_at"])
    return _request_result(request)


def _close_request(request, actor, status, *, comment=""):
    now = timezone.now()
    request.allocations.filter(status=StaffingAllocation.Status.PENDING).update(
        status=StaffingAllocation.Status.REVOKED, resolved_at=now,
    )
    request.status = status
    request.handled_by = actor
    request.handled_at = now
    request.comment = comment
    request.save(update_fields=["status", "handled_by", "handled_at", "comment", "updated_at"])
    return _request_result(request)


@transaction.atomic
def cancel_request(*, user, org_id, request_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _project_request_to_change(access, request_id)
    if request.status != StaffingRequest.Status.DRAFT:
        _open_request(request)
    return _close_request(request, user, StaffingRequest.Status.CANCELLED)


@transaction.atomic
def reject_request(*, user, org_id, request_id, comment):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _request_record(access, request_id, lock=True)
    _require_department_side(access, request)
    _active_project(request.project)
    _open_request(request)
    return _close_request(request, user, StaffingRequest.Status.REJECTED, comment=comment)


def _branch_departments(organization, department_id):
    children = defaultdict(list)
    for department in organization.departments.filter(is_archived=False):
        children[department.parent_id].append(department)
    branch = []
    pending = children[None].copy()
    # Locate the target by walking only active nodes, then walk its descendants.
    while pending:
        department = pending.pop()
        if department.pk == department_id:
            pending = [department]
            break
        pending.extend(children[department.pk])
    while pending:
        department = pending.pop()
        branch.append(department)
        pending.extend(children[department.pk])
    return branch


def _candidate_memberships(access, request):
    department_ids = [
        department.pk for department in _branch_departments(access.organization, request.department_id)
        if access.allows(ALLOCATE_PERMISSION, department)
    ]
    return DepartmentMembership.objects.filter(
        organization=access.organization, department_id__in=department_ids,
        department__is_archived=False, is_active=True, organization_membership__is_active=True,
        organization_membership__user__is_active=True,
    ).select_related("department", "organization_membership__user")


def _active_team_ids(request):
    return request.project.memberships.filter(is_active=True).values("organization_membership_id")


def _pending_project_ids(request):
    return request.project.staffing_allocations.filter(
        status=StaffingAllocation.Status.PENDING,
    ).values("organization_membership_id")


def list_candidates(*, user, org_id, request_id):
    access = organization_access(user=user, org_id=org_id)
    request = _request_record(access, request_id)
    _require_department_side(access, request)
    _active_project(request.project)
    _open_request(request)
    memberships = list(_candidate_memberships(access, request).exclude(
        organization_membership_id__in=_active_team_ids(request),
    ).exclude(
        organization_membership_id__in=_pending_project_ids(request),
    ).order_by("id"))
    member_ids = {membership.organization_membership_id for membership in memberships}
    projects = {project.pk: project.name for project in visible_projects(access=access)}
    visible_assignments = defaultdict(list)
    for member_id, project_id in ProjectMembership.objects.filter(
        organization=access.organization, organization_membership_id__in=member_ids,
        project_id__in=projects, is_active=True,
    ).order_by("project_id").values_list("organization_membership_id", "project_id"):
        visible_assignments[member_id].append({"id": project_id, "name": projects[project_id]})
    return [
        {
            "department_membership_id": membership.pk,
            "organization_membership_id": membership.organization_membership_id,
            "username": membership.organization_membership.user.get_username(),
            "department_id": membership.department_id,
            "department_name": membership.department.name,
            "visible_projects": visible_assignments[membership.organization_membership_id],
        }
        for membership in memberships
    ]


def _allocation_status(accepted_count, requested_count):
    if accepted_count >= requested_count:
        return StaffingRequest.Status.FULFILLED
    if accepted_count:
        return StaffingRequest.Status.PARTIAL
    return StaffingRequest.Status.SENT


def _refresh_request(request, actor=None):
    result = _request_result(request)
    request.status = _allocation_status(result.accepted_count, result.requested_count)
    fields = ["status", "updated_at"]
    if actor is not None:
        request.handled_by = actor
        request.handled_at = timezone.now()
        fields.extend(["handled_by", "handled_at"])
    request.save(update_fields=fields)
    return _request_result(request)


def _activate_membership(request, member):
    membership, created = request.project.memberships.get_or_create(
        organization=request.organization,
        organization_membership=member.organization_membership,
        defaults={"source_department": member.department},
    )
    if not created:
        if membership.is_active:
            raise StaffingConflict("Пользователь уже состоит в проекте.")
        membership.source_department = member.department
        membership.is_active = True
        membership.ended_at = None
        membership.save(update_fields=["source_department", "is_active", "ended_at"])


@transaction.atomic
def allocate(*, user, org_id, request_id, department_membership_ids):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _request_record(access, request_id, lock=True)
    _require_department_side(access, request)
    _active_project(request.project)
    _open_request(request)

    requested_ids = set(department_membership_ids)
    if not requested_ids or len(requested_ids) != len(department_membership_ids):
        raise StaffingConflict("Выберите уникальных кандидатов для назначения.")
    candidates = list(_candidate_memberships(access, request).filter(pk__in=requested_ids))
    if len(candidates) != len(requested_ids):
        # Never disclose whether an unselectable ID belongs to another tenant.
        raise Http404
    member_ids = {candidate.organization_membership_id for candidate in candidates}
    if len(member_ids) != len(candidates):
        raise StaffingConflict("Каждого участника можно выбрать только один раз.")
    current = _request_result(request)
    if current.accepted_count + current.pending_count + len(candidates) > current.requested_count:
        raise StaffingConflict("Количество назначений превышает оставшиеся места.")
    if _active_team_ids(request).filter(organization_membership_id__in=member_ids).exists():
        raise StaffingConflict("Некоторые кандидаты уже входят в команду проекта.")
    if _pending_project_ids(request).filter(organization_membership_id__in=member_ids).exists():
        raise StaffingConflict("Некоторым кандидатам уже отправлено приглашение в этот проект.")

    mode = access.organization.project_assignment_mode
    direct = mode == Organization.ProjectAssignmentMode.DIRECT
    now = timezone.now()
    for candidate in candidates:
        if direct:
            _activate_membership(request, candidate)
        request.allocations.create(
            organization=access.organization, project=request.project,
            organization_membership=candidate.organization_membership,
            department_membership=candidate, allocated_by=user, mode=mode,
            status=StaffingAllocation.Status.ACCEPTED if direct else StaffingAllocation.Status.PENDING,
            resolved_at=now if direct else None,
        )
    return _refresh_request(request, user)


def _resolve_allocation(allocation, status):
    allocation.status = status
    allocation.resolved_at = timezone.now()
    allocation.save(update_fields=["status", "resolved_at"])


@transaction.atomic
def revoke_invitation(*, user, org_id, request_id, allocation_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    request = _request_record(access, request_id, lock=True)
    _require_department_side(access, request)
    _active_project(request.project)
    _open_request(request)
    allocation = get_object_or_404(
        request.allocations.select_related("department_membership__department"),
        organization=access.organization, pk=allocation_id,
    )
    if not access.allows(ALLOCATE_PERMISSION, allocation.department_membership.department):
        raise Http404
    if allocation.status != StaffingAllocation.Status.PENDING:
        raise StaffingConflict("Приглашение уже закрыто.")
    _resolve_allocation(allocation, StaffingAllocation.Status.REVOKED)
    return _refresh_request(request, user)


def own_invitations(*, user):
    if not user.is_authenticated or not user.is_active:
        return StaffingAllocation.objects.none()
    return StaffingAllocation.objects.filter(
        organization_membership__user=user, organization_membership__is_active=True,
        mode=Organization.ProjectAssignmentMode.INVITATION,
    ).select_related(
        "project", "organization", "request", "request__department",
        "department_membership__department", "organization_membership__user", "allocated_by",
    ).order_by("-id")


@transaction.atomic
def respond_to_invitation(*, user, allocation_id, accept):
    locator = get_object_or_404(
        own_invitations(user=user).values("organization_id", "request_id"), pk=allocation_id,
    )
    access = organization_access(user=user, org_id=locator["organization_id"], lock=True)
    request = _request_record(access, locator["request_id"], lock=True)
    allocation = get_object_or_404(
        own_invitations(user=user), pk=allocation_id, request=request, organization=access.organization,
    )
    if allocation.status != StaffingAllocation.Status.PENDING:
        raise StaffingConflict("Приглашение уже закрыто.")
    _active_project(request.project)
    _open_request(request)

    status = StaffingAllocation.Status.DECLINED
    if accept:
        candidate = allocation.department_membership
        branch_ids = {department.pk for department in _branch_departments(
            access.organization, request.department_id,
        )}
        if not candidate.is_active or candidate.department_id not in branch_ids:
            raise StaffingConflict("Исходное членство в подразделении больше недоступно.")
        _activate_membership(request, candidate)
        status = StaffingAllocation.Status.ACCEPTED
    _resolve_allocation(allocation, status)
    _refresh_request(request)
    return allocation


@transaction.atomic
def end_membership(*, user, org_id, project_id, membership_id):
    access = organization_access(user=user, org_id=org_id, lock=True)
    project = _project(access, project_id, lock=True)
    if not _can_view(access, project):
        raise Http404
    if not access.allows(TEAM_MANAGE_PERMISSION, _context(project)):
        raise PermissionDenied
    _active_project(project)
    membership = get_object_or_404(project.memberships, organization=access.organization, pk=membership_id)
    if membership.is_active:
        membership.is_active = False
        membership.ended_at = timezone.now()
        membership.save(update_fields=["is_active", "ended_at"])


@transaction.atomic
def set_assignment_mode(*, user, org_id, mode):
    access = organization_access(user=user, org_id=org_id, lock=True)
    if not access.is_owner:
        raise PermissionDenied
    if mode not in Organization.ProjectAssignmentMode.values:
        raise StaffingConflict("Выберите допустимый режим назначения.")
    access.organization.project_assignment_mode = mode
    access.organization.save(update_fields=["project_assignment_mode"])
