from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.access.services import organization_access
from apps.projects.services import visible_project

from .distribution import can_open_distribution
from .reviews import can_open_review_inbox, visible_workflow_task
from .services import can_open_project_tasks, can_open_tasks


def _context(access):
    org_id = access.organization.pk
    api_list_url = reverse("api-1.0.0:task_list", kwargs={"org_id": org_id})
    return {
        "organization": access.organization,
        "api_org_url": api_list_url.removesuffix("tasks/"),
        "task_list_url": reverse("organization-tasks", kwargs={"org_id": org_id}),
    }


@login_required
@require_GET
def organization_tasks(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    if not can_open_tasks(access):
        raise Http404
    return render(request, "tasks/list.html", {**_context(access), "mine": False})


@login_required
@require_GET
def organization_my_tasks(request, org_id):
    access = organization_access(user=request.user, org_id=org_id)
    return render(request, "tasks/list.html", {**_context(access), "mine": True})


@login_required
@require_GET
def project_tasks(request, org_id, project_id):
    access = organization_access(user=request.user, org_id=org_id)
    project = visible_project(access=access, project_id=project_id)
    if not can_open_project_tasks(access, project):
        raise Http404
    return render(request, "tasks/list.html", {
        **_context(access), "mine": False, "project_id": project.pk, "project": project,
    })


@login_required
@require_GET
def organization_task(request, org_id, task_id):
    access = organization_access(user=request.user, org_id=org_id)
    task = visible_workflow_task(access=access, task_id=task_id)
    return render(request, "tasks/detail.html", {**_context(access), "task_id": task.pk})


@login_required
@require_GET
def organization_task_queue(request, org_id, queue):
    access = organization_access(user=request.user, org_id=org_id)
    can_open = can_open_distribution if queue == "distribution" else can_open_review_inbox
    if not can_open(access):
        raise Http404
    return render(request, "tasks/list.html", {**_context(access), "mine": False, "queue": queue})
