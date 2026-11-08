from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.policy import Permission
from apps.access.services import organization_access
from apps.organizations.services import visible_organizations


@login_required
@require_GET
def notifications_page(request):
    return render(request, "notifications/index.html", {
        "notification_api_url": reverse("api-1.0.0:notification_list"),
    })


def _settings_api_prefix():
    return reverse("api-1.0.0:notification_policy", kwargs={"org_id": 1}).removesuffix("1/notification-policy/")


@login_required
@require_GET
def notification_policy_page(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not access.allows(Permission.NOTIFICATIONS_MANAGE):
        raise PermissionDenied
    return render(request, "notifications/settings.html", {
        "organization": access.organization,
        "personal": False,
        "settings_api_url": _settings_api_prefix(),
    })


@login_required
@require_GET
def notification_preferences_page(request):
    return render(request, "notifications/settings.html", {
        "organizations": visible_organizations(request.user).order_by("name", "pk"),
        "personal": True,
        "settings_api_url": _settings_api_prefix(),
    })
