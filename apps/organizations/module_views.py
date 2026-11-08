from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.policy import Permission
from apps.access.services import organization_access

from .modules import ModuleDisabled


class ModuleAvailabilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, ModuleDisabled):
            response = render(request, "organizations/module_unavailable.html", status=409)
            response["Cache-Control"] = "no-store"
            return response
        return None


@login_required
@require_GET
def organization_modules(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not access.allows(Permission.MODULES_MANAGE):
        raise PermissionDenied
    response = render(request, "organizations/modules.html", {
        "organization": access.organization,
        "modules_api_url": reverse("api-1.0.0:organization_modules", kwargs={"org_id": org_id}),
    })
    response["Cache-Control"] = "no-store"
    return response
