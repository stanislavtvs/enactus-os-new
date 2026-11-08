from collections import defaultdict

from django.urls import reverse

from apps.access.policy import Permission
from apps.calendar_app.invitation_services import can_manage_invitations
from apps.calendar_app.services import _can_read_event, _event_rows
from apps.departments.models import DepartmentInvitation, DepartmentMembership
from apps.departments.services import can_view_department_directory
from apps.notifications.models import OrganizationNotificationPolicy
from apps.notifications.visibility import _project_targets, _staffing_targets
from apps.projects.models import StaffingAllocation
from apps.projects.staffing import can_view_team
from apps.tasks.services import _task_rows, _task_visible


TEAM_ACTIONS = {"project.member_added", "project.member_removed"}


def _workspace_target(access, route, title):
    return title, reverse(route, args=[access.organization.pk])


def _objects(access, groups, departments, projects):
    organization = access.organization
    targets = {
        "task": {
            row.pk: (row.name, reverse("organization-task", args=[organization.pk, row.pk]))
            for row in _task_rows(access).filter(pk__in=groups["task"]).prefetch_related(None)
            if _task_visible(access, row)
        } if groups["task"] else {},
        "project": projects,
        "department": {
            pk: _workspace_target(access, "organization-departments", row.name)
            for pk, row in departments.items()
        },
        "organization": {organization.pk: _workspace_target(access, "organization-detail", organization.name)},
    }
    if access.allows(Permission.ORGANIZATIONS_ONBOARDING_MANAGE):
        for kind, rows in (
            ("organization_invitation", organization.invitations),
            ("join_request", organization.join_requests),
        ):
            if groups[kind]:
                targets[kind] = dict.fromkeys(
                    rows.filter(pk__in=groups[kind]).values_list("pk", flat=True),
                    _workspace_target(access, "organization-manage", organization.name),
                )
    if access.allows(Permission.ROLES_READ) or access.allows(Permission.ROLES_MANAGE):
        for kind, rows, field in (
            ("role", organization.roles, "name"),
            ("role_assignment", organization.role_assignments, "role__name"),
            ("position_assignment", organization.position_assignments, "title"),
        ):
            if groups[kind]:
                targets[kind] = {
                    pk: _workspace_target(access, "organization-access", title)
                    for pk, title in rows.filter(pk__in=groups[kind]).values_list("pk", field)
                }
    for kind, model in (
        ("department_invitation", DepartmentInvitation), ("department_membership", DepartmentMembership),
    ):
        if not groups[kind]:
            continue
        rows = model.objects.filter(organization=organization, pk__in=groups[kind]).select_related("department")
        targets[kind] = {
            row.pk: (
                row.department.name, reverse("department-members", args=[organization.pk, row.department_id]),
            )
            for row in rows
            if (
                can_view_department_directory(access, row.department)
                if kind == "department_membership"
                else row.organization_membership_id == access.membership.pk
                or access.allows(Permission.DEPARTMENTS_INVITATIONS_MANAGE, row.department)
            )
        }

    request_ids = set(groups["staffing_request"])
    allocations = []
    if groups["staffing_allocation"]:
        allocations = list(StaffingAllocation.objects.filter(
            organization=organization, pk__in=groups["staffing_allocation"],
        ).values_list("pk", "request_id"))
        request_ids.update(request_id for _, request_id in allocations)
    requests = _staffing_targets(access, request_ids) if request_ids else {}
    targets["staffing_request"] = requests
    targets["staffing_allocation"] = {
        pk: requests[request_id] for pk, request_id in allocations if request_id in requests
    }
    if groups["notification_policy"] and access.allows(Permission.NOTIFICATIONS_MANAGE):
        targets["notification_policy"] = dict.fromkeys(
            OrganizationNotificationPolicy.objects.filter(
                organization=organization, pk__in=groups["notification_policy"],
            ).values_list("pk", flat=True),
            _workspace_target(access, "organization-notification-policy", "Уведомления организации"),
        )
    return targets


def present_events(access, events):
    groups = defaultdict(set)
    department_ids = set()
    project_ids = set()
    team_project_ids = set()
    for event in events:
        groups[event.object_type].add(event.object_id)
        department_ids.update(event.metadata.get("affected_department_ids") or [])
        project_ids.update(event.metadata.get("affected_project_ids") or [])
        if event.object_type == "task":
            for field in ("parent_id", "predecessor_id"):
                value = event.metadata.get(field)
                if type(value) is int:
                    groups["task"].add(value)
            for field in ("parent_id", "predecessor_id", "child_id"):
                for value in event.changes.get(field, {}).values():
                    if type(value) is int:
                        groups["task"].add(value)
        if event.department_id:
            department_ids.add(event.department_id)
        if event.object_type == "department":
            department_ids.add(event.object_id)
        if event.project_id:
            project_ids.add(event.project_id)
        if event.action in TEAM_ACTIONS:
            team_project_ids.add(event.object_id)
    project_ids.update(groups["project"])
    departments = {
        row.pk: row for row in access.organization.departments.filter(pk__in=department_ids)
        if access.is_owner or access.allows(Permission.DEPARTMENTS_READ, row)
    }
    projects = _project_targets(access, project_ids) if project_ids else {}
    targets = _objects(access, groups, departments, projects)
    calendar = {}
    if groups["calendar_event"]:
        calendar = {
            row.pk: row for row in _event_rows(access).filter(pk__in=groups["calendar_event"])
            if _can_read_event(access, row)
        }
    if team_project_ids:
        from apps.projects.services import _projects

        teams = {row.pk for row in _projects(access).filter(pk__in=team_project_ids) if can_view_team(access, row)}
    else:
        teams = set()
    result = {}
    for event in events:
        if event.department_id and event.department_id not in departments:
            continue
        if event.project_id and event.project_id not in projects and event.object_type not in {
            "staffing_request", "staffing_allocation",
        }:
            continue
        if not projects.keys() >= set(event.metadata.get("affected_project_ids") or []):
            continue
        if event.action in TEAM_ACTIONS and event.object_id not in teams:
            continue
        if event.object_type == "calendar_event":
            row = calendar.get(event.object_id)
            if row is None:
                continue
            if event.action.startswith(("calendar_event.rsvp", "calendar_event.invitation")):
                own_rsvp = event.action.startswith("calendar_event.rsvp") and event.actor_id == access.user.pk
                if not own_rsvp and not can_manage_invitations(access, row):
                    continue
            target = _workspace_target(access, "organization-calendar", row.title)
        else:
            target = targets.get(event.object_type, {}).get(event.object_id)
        if target is None:
            continue
        changes = event.changes
        metadata = event.metadata
        if event.object_type == "task":
            metadata = dict(metadata)
            for field, references in (
                ("parent_id", ("checklist_id", "item_id")),
                ("predecessor_id", ("dependency_id",)),
            ):
                if field in metadata and metadata[field] not in targets["task"]:
                    for key in (field, *references):
                        metadata.pop(key, None)
            changes = {
                field: {
                    key: value if type(value) is int and value in targets["task"] else None
                    for key, value in pair.items()
                } if field in {"parent_id", "predecessor_id", "child_id"} else pair
                for field, pair in changes.items()
            }
        result[event.pk] = {
            "detail_changes": changes,
            "detail_metadata": metadata,
            "object_title": target[0],
            "object_url": target[1],
            "department_name": departments[event.department_id].name if event.department_id else None,
            "project_name": projects.get(event.project_id, (None,))[0],
            "can_view_object": True,
        }
    return result


def can_open_object_history(access, object_type, object_id):
    if object_type == "task":
        task = _task_rows(access).filter(pk=object_id).first()
        return task is not None and _task_visible(access, task)
    if object_type == "project":
        return bool(_project_targets(access, [object_id]))
    if object_type == "department":
        department = access.organization.departments.filter(pk=object_id).first()
        return department is not None and (access.is_owner or access.allows(Permission.DEPARTMENTS_READ, department))
    if object_type == "calendar_event":
        event = _event_rows(access).filter(pk=object_id).first()
        return event is not None and _can_read_event(access, event)
    return False
