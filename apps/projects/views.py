from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from apps.access.services import organization_access
from apps.organizations.models import Organization
from apps.organizations.modules import enabled, require_module
from apps.tasks.reviews import can_manage_project_review, set_project_review_policy
from apps.tasks.services import TaskConflict, can_open_project_tasks, list_tasks

from .services import can_open_projects, visible_project
from .staffing import can_request_staffing, can_view_team


class ProjectTaskReviewForm(forms.Form):
    review_policy = forms.ChoiceField(
        label="Маршрут проверки задач",
        choices=[("", "Как в организации"), *Organization.TaskReviewPolicy.choices],
        required=False,
    )


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
    access = organization_access(user=request.user, org_id=org_id, module="projects")
    if not can_open_projects(access):
        raise Http404
    return render(request, "projects/list.html", _project_context(access))


@login_required
@require_http_methods(["GET", "POST"])
def organization_project(request, org_id, project_id):
    access = organization_access(user=request.user, org_id=org_id, module="projects")
    project = visible_project(access=access, project_id=project_id)
    review_form = None
    if enabled(access.organization, "tasks") and can_manage_project_review(access, project):
        review_form = ProjectTaskReviewForm(initial={"review_policy": project.task_review_policy})
    response_status = 200
    if request.method == "POST":
        require_module(access.organization, "tasks")
        if review_form is None:
            raise PermissionDenied
        if request.POST.get("action") != "task_review_policy":
            return HttpResponseBadRequest()
        review_form = ProjectTaskReviewForm(request.POST)
        response_status = 400
        if review_form.is_valid():
            try:
                set_project_review_policy(
                    user=request.user, org_id=org_id, project_id=project_id,
                    policy=review_form.cleaned_data["review_policy"] or None,
                )
            except TaskConflict as exc:
                review_form.add_error(None, str(exc))
                response_status = 409
            else:
                return redirect("organization-project", org_id=org_id, project_id=project_id)
    can_open_tasks = can_open_project_tasks(access, project)
    task_preview = []
    if can_open_tasks:
        task_preview, _ = list_tasks(access=access, project_id=project.pk, page_size=5)
    return render(request, "projects/detail.html", {
        **_project_context(access), "project_id": project.pk,
        "can_open_project_tasks": can_open_tasks,
        "project_tasks_preview": task_preview,
        "review_form": review_form,
        "can_open_team": can_view_team(access, project) or can_request_staffing(access, project),
    }, status=response_status)
