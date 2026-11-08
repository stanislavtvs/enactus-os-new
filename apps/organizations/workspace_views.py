from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.access.policy import Access

from .onboarding import OnboardingError, set_join_policy
from .onboarding_forms import MAX_ID, PolicyForm
from .services import visible_organizations
from .workspaces import ACTIVE_WORKSPACE_KEY, can_view_organization_members


class WorkspaceSwitchForm(forms.Form):
    organization_id = forms.IntegerField(min_value=1, max_value=MAX_ID)


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
    if request.method == "POST":
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
    else:
        form = PolicyForm(initial={"policy": organization.join_policy})

    return render(request, "organizations/settings.html", {
        "organization": organization,
        "policy_form": form,
    }, status=status)


@login_required
@require_GET
def user_profile(request):
    return render(request, "accounts/profile.html", {"profile_user": request.user})


@login_required
@require_GET
def global_settings(request):
    return render(request, "accounts/settings.html", {"profile_user": request.user})
