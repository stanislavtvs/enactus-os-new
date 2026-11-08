from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.services import organization_access

from .services import can_open_calendar


@login_required
@require_GET
def organization_calendar(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not can_open_calendar(access):
        raise Http404
    feed_url = reverse("api-1.0.0:calendar_feed", kwargs={"org_id": org_id})
    return render(request, "calendar_app/index.html", {
        "organization": access.organization,
        "api_org_url": feed_url.removesuffix("calendar/"),
    })
