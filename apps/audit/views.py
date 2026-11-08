from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.services import organization_access

from .services import can_open_audit


@login_required
@require_GET
def audit_page(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not can_open_audit(access):
        raise PermissionDenied
    response = render(request, "audit/index.html", {
        "organization": access.organization,
        "audit_api_url": reverse("api-1.0.0:audit_list", kwargs={"org_id": org_id}),
    })
    response["Cache-Control"] = "no-store"
    return response
