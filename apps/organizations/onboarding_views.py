from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .join_links import make_join_code, read_join_code
from .models import JoinRequest, Organization, OrganizationInvitation, OrganizationMembership
from .onboarding import (
    OnboardingError,
    OnboardingRateLimit,
    can_manage_members,
    issue_invitation,
    respond_to_invitation,
    review_join_request,
    revoke_invitation,
    set_join_policy,
    set_member_manager,
    submit_join_request,
)
from .onboarding_forms import (
    InvitationResponseForm,
    InviteForm,
    ManagerForm,
    PolicyForm,
    ReviewForm,
    RevokeForm,
)
from .services import visible_organizations

MANAGE_FORMS = {
    "policy": PolicyForm,
    "invite": InviteForm,
    "revoke": RevokeForm,
    "review": ReviewForm,
    "manager": ManagerForm,
}
REQUEST_POLICIES = (Organization.JoinPolicy.REQUESTS, Organization.JoinPolicy.BOTH)


def _manage_action(action, actor, org_id, data):
    if action == "policy":
        set_join_policy(actor=actor, org_id=org_id, policy=data["policy"])
    elif action == "invite":
        _, token = issue_invitation(actor=actor, org_id=org_id, username=data["username"])
        return token
    elif action == "revoke":
        revoke_invitation(actor=actor, org_id=org_id, invitation_id=data["invitation_id"])
    elif action == "review":
        review_join_request(
            actor=actor, org_id=org_id, request_id=data["request_id"],
            approve=data["decision"] == "approve",
        )
    else:
        set_member_manager(
            actor=actor, org_id=org_id, membership_id=data["membership_id"],
            enabled=data["enabled"] == "yes",
        )
    return None


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def organization_manage(request, org_id):
    organization = get_object_or_404(visible_organizations(request.user), pk=org_id)
    if not can_manage_members(request.user, organization):
        raise PermissionDenied

    policy_form = PolicyForm(initial={"policy": organization.join_policy})
    invite_form = InviteForm()
    token = None
    error = None
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in MANAGE_FORMS:
            return HttpResponseBadRequest("Неизвестное действие.")
        form = MANAGE_FORMS[action](request.POST)
        if action == "policy":
            policy_form = form
        elif action == "invite":
            invite_form = form
        if form.is_valid():
            try:
                token = _manage_action(action, request.user, org_id, form.cleaned_data)
            except OnboardingRateLimit as exc:
                error, status = str(exc), 429
            except OnboardingError as exc:
                error, status = str(exc), 409
            else:
                if token is None:
                    return redirect("organization-manage", org_id=org_id)
                invite_form = InviteForm()
        else:
            error, status = "Проверьте введённые данные.", 400

    now = timezone.now()
    if organization.join_policy == Organization.JoinPolicy.REQUESTS and not invite_form.is_bound:
        invite_form = None
    invitations = organization.invitations.select_related("recipient").order_by("-created_at", "-id")
    for invitation in invitations:
        if invitation.status == OrganizationInvitation.Status.PENDING and invitation.expires_at <= now:
            invitation.status = OrganizationInvitation.Status.EXPIRED
    invitation_link = (
        request.build_absolute_uri(reverse("organization-invitations")) + "#" + token
        if token else None
    )
    join_link = (
        request.build_absolute_uri(reverse("organization-join", args=[make_join_code(org_id)]))
        if organization.join_policy in REQUEST_POLICIES else None
    )
    return render(request, "organizations/manage.html", {
        "organization": organization,
        "members": organization.memberships.filter(is_active=True).select_related("user").order_by("user__username"),
        "invitations": invitations,
        "join_requests": organization.join_requests.select_related("applicant").order_by("-created_at", "-id"),
        "can_edit_policy": request.user.pk == organization.owner_id,
        "policy_form": policy_form,
        "invite_form": invite_form,
        "invitation_link": invitation_link,
        "join_link": join_link,
        "error": error,
    }, status=status)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def organization_invitations(request):
    error = None
    status = 200
    if request.method == "POST":
        form = InvitationResponseForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                respond_to_invitation(
                    actor=request.user, invitation_id=data["invitation_id"], token=data["token"] or None,
                    accept=data["decision"] == "accept",
                )
            except OnboardingError as exc:
                error, status = str(exc), 409
            else:
                return redirect("organization-invitations")
        else:
            error, status = "Проверьте данные приглашения.", 400

    invitations = OrganizationInvitation.objects.filter(
        recipient=request.user,
        status=OrganizationInvitation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).select_related("organization").order_by("-created_at", "-id")
    return render(request, "organizations/invitations.html", {
        "invitations": invitations, "error": error,
    }, status=status)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def organization_join(request, code):
    organization = get_object_or_404(
        Organization, pk=read_join_code(code), join_policy__in=REQUEST_POLICIES,
    )
    error = None
    status = 200
    if request.method == "POST":
        try:
            submit_join_request(actor=request.user, org_id=organization.pk)
        except OnboardingRateLimit as exc:
            error, status = str(exc), 429
        except OnboardingError as exc:
            error, status = str(exc), 409
        else:
            return redirect("organization-join", code=code)

    submitted = JoinRequest.objects.filter(
        organization=organization, applicant=request.user, status=JoinRequest.Status.PENDING,
    ).exists()
    is_member = OrganizationMembership.objects.filter(
        organization=organization, user=request.user, is_active=True,
    ).exists()
    return render(request, "organizations/join.html", {
        "organization": organization, "submitted": submitted, "is_member": is_member, "error": error,
    }, status=status)
