from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from apps.access.policy import Access
from apps.projects.services import can_open_projects

from .onboarding import can_manage_members
from .services import can_create_organization, visible_organizations


@login_required
def organization_list(request):
    return render(
        request,
        "organizations/list.html",
        {
            "organizations": visible_organizations(request.user),
            "can_create_organization": can_create_organization(request.user),
            "organization_api_url": reverse("api-1.0.0:create_organization"),
        },
    )


@login_required
def organization_detail(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    return render(request, "organizations/detail.html", {
        "organization": organization,
        "can_manage_members": can_manage_members(request.user, organization),
        "can_open_projects": can_open_projects(Access(request.user, organization)),
    })
