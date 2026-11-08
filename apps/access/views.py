from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .services import organization_access


@login_required
@require_GET
def organization_access_page(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    can_manage = access.allows("roles.manage")
    return render(request, "access/index.html", {
        "organization": access.organization,
        "can_view_roles": can_manage or access.allows("roles.read"),
        "can_manage_roles": can_manage,
        "is_owner": access.is_owner,
        "access_api_url": reverse(
            "api-1.0.0:role_list", kwargs={"org_id": org_id},
        ).removesuffix("roles/"),
    })
