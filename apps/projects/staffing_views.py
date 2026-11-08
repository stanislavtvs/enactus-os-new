from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.services import organization_access

from . import staffing
from .services import visible_project


def _org_api(org_id):
    return reverse("api-1.0.0:project_list", kwargs={"org_id": org_id}).removesuffix("projects/")


@login_required
@require_GET
def project_team_page(request, org_id, project_id):
    access = organization_access(user=request.user, org_id=org_id)
    project = visible_project(access=access, project_id=project_id)
    if not (
        staffing.can_view_team(access, project)
        or staffing.can_request_staffing(access, project)
    ):
        raise Http404
    return render(request, "staffing/team.html", {
        "organization": access.organization,
        "project": project,
        "api_org_url": _org_api(org_id),
        "api_project_url": reverse("api-1.0.0:project_detail", kwargs={
            "org_id": org_id, "project_id": project_id,
        }),
    })


@login_required
@require_GET
def organization_staffing_page(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not staffing.can_open_staffing(access):
        raise Http404
    return render(request, "staffing/queue.html", {
        "organization": access.organization, "api_org_url": _org_api(org_id),
    })


@login_required
@require_GET
def staffing_request_page(request, org_id, request_id):
    item = staffing.get_request(user=request.user, org_id=org_id, request_id=request_id)
    return render(request, "staffing/request.html", {
        "organization": item.organization, "staffing_request": item,
        "api_org_url": _org_api(org_id),
    })


@login_required
@require_GET
def project_invitation_page(request):
    return render(request, "staffing/invitations.html", {
        "project_invitation_api_url": reverse("api-1.0.0:project_invitation_inbox"),
    })
