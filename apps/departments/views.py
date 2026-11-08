from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.policy import Access
from apps.organizations.models import Organization
from apps.organizations.services import visible_organizations

from .membership_services import get_directory_department
from .services import department_actions


@login_required
@require_GET
def department_structure(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    actions = department_actions(user=request.user, org_id=org_id)
    return render(request, "departments/structure.html", {
        "organization": organization,
        "can_manage_departments": actions["root"]["create"] or any(
            any(permissions.values()) for permissions in actions["nodes"].values()
        ),
        "department_actions_url": reverse("api-1.0.0:department_actions", kwargs={"org_id": org_id}),
        "department_api_url": reverse("api-1.0.0:department_list", kwargs={"org_id": org_id}),
    })


@login_required
@require_GET
def department_members(request, org_id, department_id):
    department = get_directory_department(
        user=request.user, org_id=org_id, department_id=department_id,
    )
    organization = department.organization
    access = Access(request.user, organization)
    return render(request, "departments/members.html", {
        "organization": organization,
        "department": department,
        "can_manage_members": access.allows("departments.invitations.manage", department),
        "can_see_other_members": (
            organization.department_directory_visibility == Organization.DepartmentDirectoryVisibility.ORGANIZATION
            or access.allows("departments.directory.read")
        ),
        "manageable_department_ids": [
            item.pk for item in organization.departments.filter(is_archived=False)
            if access.allows("departments.members.manage", item)
        ],
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
