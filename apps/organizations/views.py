from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_GET
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.access.policy import Access
from apps.projects.services import can_open_projects, visible_projects

from .dashboard import dashboard
from .platform import creation_policy
from .platform_access import is_platform_admin
from .services import visible_organizations
from .workspaces import can_view_organization_members, current_workspace


@login_required
def home(request):
    organization = current_workspace(request)
    if organization is not None:
        return redirect("organization-detail", org_id=organization.pk)
    return organization_list(request)


@login_required
def organization_list(request):
    policy = creation_policy(request.user)
    return render(
        request,
        "organizations/list.html",
        {
            "organizations": visible_organizations(request.user),
            "can_create_organization": policy["can_create"],
            "creation_policy": policy,
            "platform_api_url": reverse("api-1.0.0:creation_policy").removesuffix("creation-policy/"),
            "organization_api_url": reverse("api-1.0.0:create_organization"),
        },
    )


@login_required
@require_GET
def platform_management(request):
    if not is_platform_admin(request.user):
        raise PermissionDenied
    response = render(request, "organizations/platform.html", {
        "platform_api_url": reverse("api-1.0.0:creation_policy").removesuffix("creation-policy/"),
    })
    response["Cache-Control"] = "no-store"
    return response


@login_required
def organization_detail(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    access = Access(request.user, organization)
    projects = visible_projects(access=access) if can_open_projects(access) else None
    return render(request, "organizations/detail.html", {
        "organization": organization,
        "department_count": organization.departments.filter(is_archived=False).count(),
        "member_count": (
            organization.memberships.filter(is_active=True, user__is_active=True).count()
            if can_view_organization_members(access) else None
        ),
        "project_count": sum(not project.is_archived for project in projects) if projects is not None else None,
        **dashboard(access, projects or []),
    })
