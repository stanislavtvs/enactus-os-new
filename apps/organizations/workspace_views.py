from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.access.policy import Access
from apps.projects.staffing import StaffingConflict, set_assignment_mode
from apps.tasks.reviews import set_organization_review_policy

from .models import Organization
from .onboarding import OnboardingError, set_join_policy
from .onboarding_forms import MAX_ID, PolicyForm
from .services import visible_organizations
from .workspaces import ACTIVE_WORKSPACE_KEY, can_view_organization_members


class WorkspaceSwitchForm(forms.Form):
    organization_id = forms.IntegerField(min_value=1, max_value=MAX_ID)


class ProjectAssignmentModeForm(forms.Form):
    mode = forms.ChoiceField(
        label="Способ включения в проект", choices=Organization.ProjectAssignmentMode.choices,
    )


class TaskAssignmentModeForm(forms.Form):
    task_mode = forms.ChoiceField(
        label="Способ назначения задач", choices=Organization.TaskAssignmentMode.choices,
    )


class TaskReviewPolicyForm(forms.Form):
    review_policy = forms.ChoiceField(
        label="Маршрут проверки по умолчанию", choices=Organization.TaskReviewPolicy.choices,
    )


@login_required
@require_POST
def organization_switch(request):
    form = WorkspaceSwitchForm(request.POST)
    if not form.is_valid():
        raise Http404
    organization = get_object_or_404(
        visible_organizations(request.user), pk=form.cleaned_data["organization_id"],
    )
    request.session[ACTIVE_WORKSPACE_KEY] = organization.pk
    return redirect("organization-detail", org_id=organization.pk)


@login_required
@require_GET
def organization_members(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    if not can_view_organization_members(Access(request.user, organization)):
        raise PermissionDenied
    members = organization.memberships.filter(
        is_active=True, user__is_active=True,
    ).order_by("user__username", "id").values(username=F("user__username"))
    return render(request, "organizations/members.html", {"organization": organization, "members": members})


@login_required
@require_http_methods(["GET", "POST"])
def organization_settings(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    if not Access(request.user, organization).is_owner:
        raise PermissionDenied

    status = 200
    assignment_form = ProjectAssignmentModeForm(initial={"mode": organization.project_assignment_mode})
    task_assignment_form = TaskAssignmentModeForm(initial={"task_mode": organization.task_assignment_mode})
    review_form = TaskReviewPolicyForm(initial={"review_policy": organization.task_review_policy})
    form = PolicyForm(initial={"policy": organization.join_policy})
    if request.method == "POST" and request.POST.get("action") == "assignment_mode":
        assignment_form = ProjectAssignmentModeForm(request.POST)
        status = 400
        if assignment_form.is_valid():
            try:
                set_assignment_mode(user=request.user, org_id=org_id, mode=assignment_form.cleaned_data["mode"])
            except StaffingConflict as exc:
                assignment_form.add_error(None, str(exc))
                status = 409
            else:
                return redirect("organization-settings", org_id=org_id)
    elif request.method == "POST" and request.POST.get("action") == "task_assignment_mode":
        task_assignment_form = TaskAssignmentModeForm(request.POST)
        status = 400
        if task_assignment_form.is_valid():
            with transaction.atomic():
                organization = get_object_or_404(
                    visible_organizations(request.user).select_for_update(of=("self",)), pk=org_id,
                )
                if not Access(request.user, organization).is_owner:
                    raise PermissionDenied
                organization.task_assignment_mode = task_assignment_form.cleaned_data["task_mode"]
                organization.save(update_fields=["task_assignment_mode"])
            return redirect("organization-settings", org_id=org_id)
    elif request.method == "POST" and request.POST.get("action") == "task_review_policy":
        review_form = TaskReviewPolicyForm(request.POST)
        status = 400
        if review_form.is_valid():
            set_organization_review_policy(
                user=request.user, org_id=org_id, policy=review_form.cleaned_data["review_policy"],
            )
            return redirect("organization-settings", org_id=org_id)
    elif request.method == "POST":
        form = PolicyForm(request.POST)
        status = 400
        if form.is_valid():
            try:
                set_join_policy(actor=request.user, org_id=org_id, policy=form.cleaned_data["policy"])
            except OnboardingError as exc:
                form.add_error(None, str(exc))
                status = 409
            else:
                return redirect("organization-settings", org_id=org_id)
    return render(request, "organizations/settings.html", {
        "organization": organization,
        "policy_form": form,
        "assignment_form": assignment_form,
        "task_assignment_form": task_assignment_form,
        "review_form": review_form,
    }, status=status)


@login_required
@require_GET
def user_profile(request):
    return render(request, "accounts/profile.html", {"profile_user": request.user})


@login_required
@require_GET
def global_settings(request):
    return render(request, "accounts/settings.html", {"profile_user": request.user})
