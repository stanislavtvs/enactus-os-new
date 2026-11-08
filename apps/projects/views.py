from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.services import organization_access
from apps.tasks.services import can_open_project_tasks, list_tasks

from .services import can_open_projects, visible_project
from .staffing import can_request_staffing, can_view_team


def _project_context(access):
    org_id = access.organization.pk
    return {
        "organization": access.organization,
        "project_api_url": reverse("api-1.0.0:project_list", kwargs={"org_id": org_id}),
        "project_options_url": reverse("api-1.0.0:project_options", kwargs={"org_id": org_id}),
    }


@login_required
@require_GET
def organization_projects(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not can_open_projects(access):
        raise Http404
    return render(request, "projects/list.html", _project_context(access))


@login_required
@require_GET
def organization_project(request, org_id, project_id):
    access = organization_access(user=request.user, org_id=org_id)
    project = visible_project(access=access, project_id=project_id)
    can_open_tasks = can_open_project_tasks(access, project)
    task_preview = []
    if can_open_tasks:
        task_preview, _ = list_tasks(access=access, project_id=project.pk, page_size=5)
    return render(request, "projects/detail.html", {
        **_project_context(access), "project_id": project.pk,
        "can_open_project_tasks": can_open_tasks,
        "project_tasks_preview": task_preview,
        "can_open_team": can_view_team(access, project) or can_request_staffing(access, project),
    })
