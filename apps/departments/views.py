from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.organizations.onboarding import can_manage_members
from apps.organizations.services import visible_organizations


@login_required
@require_GET
def department_structure(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    return render(request, "departments/structure.html", {
        "organization": organization,
        "can_manage_departments": can_manage_members(request.user, organization),
        "department_api_url": reverse("api-1.0.0:department_list", kwargs={"org_id": org_id}),
    })
