from django.urls import reverse

from apps.access.policy import Access, Permission
from apps.projects.services import can_open_projects

from .models import Organization
from .services import visible_organizations

ACTIVE_WORKSPACE_KEY = "active_workspace_id"

SCOPED_ROUTES = {
    "organization-detail",
    "organization-departments",
    "department-members",
    "organization-members",
    "organization-projects",
    "organization-project",
    "organization-access",
    "organization-manage",
    "organization-settings",
}

PAGE_TITLES = {
    "home": "Мои организации",
    "organization-list": "Мои организации",
    "organization-detail": "Обзор",
    "organization-departments": "Подразделения",
    "department-members": "Участники подразделения",
    "organization-members": "Участники",
    "organization-projects": "Проекты",
    "organization-project": "Проект",
    "organization-access": "Роли и доступ",
    "organization-manage": "Управление вступлением",
    "organization-settings": "Настройки организации",
    "organization-invitations": "Приглашения в организации",
    "department-invitations": "Приглашения в подразделения",
    "organization-join": "Вступление в организацию",
    "user-profile": "Профиль",
    "global-settings": "Настройки аккаунта",
}


def current_workspace(request, choices=None):
    workspace_id = request.session.get(ACTIVE_WORKSPACE_KEY)
    if workspace_id is None:
        return None
    workspace = None
    if type(workspace_id) is int and 0 < workspace_id <= 2**63 - 1:
        if choices is None:
            workspace = visible_organizations(request.user).filter(pk=workspace_id).first()
        else:
            workspace = choices.get(workspace_id)
    if workspace is None:
        request.session.pop(ACTIVE_WORKSPACE_KEY, None)
    return workspace


def can_view_organization_members(access):
    return access.membership is not None and (
        access.organization.department_directory_visibility
        == Organization.DepartmentDirectoryVisibility.ORGANIZATION
        or access.allows(Permission.DEPARTMENTS_DIRECTORY_READ)
    )


def _link(route, label, icon, current_route, *, org_id=None, related=()):
    kwargs = {"org_id": org_id} if org_id is not None else None
    return {
        "url": reverse(route, kwargs=kwargs),
        "label": label,
        "icon": icon,
        "active": current_route == route or current_route in related,
    }


def _workspace_links(request, workspace, route):
    if workspace is None:
        return []
    access = Access(request.user, workspace)
    scoped = (
        ("organization-detail", "Обзор", "home", True, ()),
        ("organization-departments", "Подразделения", "org", True, ("department-members",)),
        ("organization-members", "Участники", "user", can_view_organization_members(access), ()),
        ("organization-projects", "Проекты", "arrow", can_open_projects(access), ("organization-project",)),
        ("organization-access", "Роли и доступ", "org", True, ()),
        (
            "organization-manage", "Приглашения и заявки", "invite",
            access.allows(Permission.ORGANIZATIONS_ONBOARDING_MANAGE), (),
        ),
        ("organization-settings", "Настройки организации", "settings", access.is_owner, ()),
    )
    return [
        _link(name, label, icon, route, org_id=workspace.pk, related=related)
        for name, label, icon, allowed, related in scoped if allowed
    ]


class WorkspaceNavigationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        workspace_id = getattr(request, "_workspace_to_activate", None)
        if (
            200 <= response.status_code < 300
            and workspace_id is not None
            and request.session.get(ACTIVE_WORKSPACE_KEY) != workspace_id
        ):
            request.session[ACTIVE_WORKSPACE_KEY] = workspace_id
        return response


def workspace_navigation(request):
    if not request.user.is_authenticated or not request.user.is_active:
        return {}

    match = request.resolver_match
    route = match.url_name if match else None
    choices = {workspace.pk: workspace for workspace in visible_organizations(request.user)}
    workspace = current_workspace(request, choices)
    # The middleware saves this candidate only after a successful response.
    if request.method == "GET" and route in SCOPED_ROUTES:
        explicit_workspace = choices.get(match.kwargs.get("org_id"))
        if explicit_workspace is not None:
            workspace = explicit_workspace
            request._workspace_to_activate = workspace.pk

    return {
        "workspace_active": workspace,
        "workspace_choices": list(choices.values()),
        "workspace_links": _workspace_links(request, workspace, route),
        "workspace_page_title": PAGE_TITLES.get(route, "Enactus OS"),
    }
