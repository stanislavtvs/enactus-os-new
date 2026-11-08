from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.organizations.onboarding import can_manage_members
from apps.organizations.services import visible_organizations

from .membership_services import get_directory_department


@login_required
@require_GET
def department_structure(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    return render(request, "departments/structure.html", {
        "organization": organization,
        "can_manage_departments": can_manage_members(request.user, organization),
        "department_api_url": reverse("api-1.0.0:department_list", kwargs={"org_id": org_id}),
    })


@login_required
@require_GET
def department_members(request, org_id, department_id):
    department = get_directory_department(
        user=request.user, org_id=org_id, department_id=department_id,
    )
    organization = department.organization
    return render(request, "departments/members.html", {
        "organization": organization,
        "department": department,
        "can_manage_members": can_manage_members(request.user, organization),
        "can_edit_directory_policy": request.user.pk == organization.owner_id,
        "directory_visibility": organization.department_directory_visibility,
        "api_department_url": reverse(
            "api-1.0.0:department_member_list",
            kwargs={"org_id": org_id, "department_id": department_id},
        ).removesuffix("members/"),
        "api_organization_url": reverse(
            "api-1.0.0:department_list", kwargs={"org_id": org_id},
        ).removesuffix("departments/"),
    })


@login_required
@require_GET
def department_invitations(request):
    return render(request, "departments/invitations.html", {
        "department_invitation_api_url": reverse("api-1.0.0:department_invitation_inbox"),
    })
