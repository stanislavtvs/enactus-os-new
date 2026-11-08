from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts import views as account_views
from apps.access.views import organization_access_page
from apps.calendar_app.views import organization_calendar, organization_calendar_invitations
from apps.audit.views import audit_page
from apps.notifications.views import notification_policy_page, notification_preferences_page, notifications_page
from apps.departments.views import department_invitations, department_members, department_structure
from apps.organizations.module_views import organization_modules
from apps.organizations.onboarding_views import (
    organization_invitations,
    organization_join,
    organization_manage,
)
from apps.organizations.views import home, organization_detail, organization_list
from apps.organizations.workspace_views import (
    global_settings,
    organization_members,
    organization_settings,
    organization_switch,
    ownership_inbox,
    user_profile,
)

from apps.projects.staffing_views import (
    organization_staffing_page,
    project_invitation_page,
    project_team_page,
    staffing_request_page,
)
from apps.projects.views import organization_project, organization_projects

from apps.tasks.views import (
    organization_my_tasks,
    organization_task,
    organization_task_configuration,
    organization_task_queue,
    organization_tasks,
    project_tasks,
)

from .api import api

urlpatterns = [
    path("", home, name="home"),
    path("notifications/", notifications_page, name="notifications"),
    path("accounts/settings/notifications/", notification_preferences_page, name="notification-preferences"),
    path(
        "organizations/<int:org_id>/settings/notifications/", notification_policy_page,
        name="organization-notification-policy",
    ),
    path("organizations/<int:org_id>/audit/", audit_page, name="organization-audit"),
    path("organizations/", organization_list, name="organization-list"),
    path("organizations/switch/", organization_switch, name="organization-switch"),
    path("organizations/<int:org_id>/members/", organization_members, name="organization-members"),
    path("organizations/<int:org_id>/settings/modules/", organization_modules, name="organization-modules"),
    path("organizations/<int:org_id>/settings/", organization_settings, name="organization-settings"),
    path("accounts/profile/", user_profile, name="user-profile"),
    path("accounts/settings/", global_settings, name="global-settings"),
    path("organizations/<int:org_id>/", organization_detail, name="organization-detail"),
    path("organizations/<int:org_id>/manage/", organization_manage, name="organization-manage"),
    path("organizations/ownership-transfers/", ownership_inbox, name="ownership-inbox"),
    path("organizations/invitations/", organization_invitations, name="organization-invitations"),
    path("organizations/join/<str:code>/", organization_join, name="organization-join"),
    path(
        "organizations/<int:org_id>/departments/",
        department_structure,
        name="organization-departments",
    ),
    path(
        "organizations/<int:org_id>/departments/<int:department_id>/members/",
        department_members,
        name="department-members",
    ),
    path(
        "organizations/department-invitations/",
        department_invitations,
        name="department-invitations",
    ),
    path(
        "organizations/<int:org_id>/access/",
        organization_access_page,
        name="organization-access",
    ),
    path("organizations/<int:org_id>/projects/", organization_projects, name="organization-projects"),
    path(
        "organizations/<int:org_id>/projects/<int:project_id>/",
        organization_project,
        name="organization-project",
    ),
    path(
        "organizations/<int:org_id>/projects/<int:project_id>/team/",
        project_team_page, name="project-team",
    ),
    path("organizations/<int:org_id>/staffing/", organization_staffing_page, name="organization-staffing"),
    path(
        "organizations/<int:org_id>/staffing/<int:request_id>/",
        staffing_request_page, name="organization-staffing-request",
    ),
    path("organizations/project-invitations/", project_invitation_page, name="project-invitations"),
    path("organizations/<int:org_id>/tasks/", organization_tasks, name="organization-tasks"),
    path("organizations/<int:org_id>/calendar/", organization_calendar, name="organization-calendar"),
    path(
        "organizations/<int:org_id>/calendar/invitations/",
        organization_calendar_invitations, name="organization-calendar-invitations",
    ),
    path(
        "organizations/<int:org_id>/calendar/invitations/<int:invitation_id>/",
        organization_calendar_invitations, name="organization-calendar-invitation",
    ),
    path("organizations/<int:org_id>/tasks/mine/", organization_my_tasks, name="organization-my-tasks"),
    path(
        "organizations/<int:org_id>/tasks/distribution/", organization_task_queue,
        {"queue": "distribution"}, name="organization-task-distribution",
    ),
    path(
        "organizations/<int:org_id>/tasks/reviews/", organization_task_queue,
        {"queue": "review"}, name="organization-task-reviews",
    ),
    path(
        "organizations/<int:org_id>/tasks/configuration/", organization_task_configuration,
        name="organization-task-configuration",
    ),
    path("organizations/<int:org_id>/tasks/<int:task_id>/", organization_task, name="organization-task"),
    path("organizations/<int:org_id>/projects/<int:project_id>/tasks/", project_tasks, name="project-tasks"),
    path("admin/", admin.site.urls),
    path("accounts/login/", account_views.AccountLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/register/", account_views.register, name="register"),
    path("accounts/verification-sent/", account_views.verification_sent, name="verification_sent"),
    path("accounts/verify-email/", account_views.verify_email, name="verify_email"),
    path("accounts/verify-email/<str:token>/", account_views.verify_email_token, name="verify_email_token"),
    path("accounts/resend-verification/", account_views.resend_verification, name="resend_verification"),
    path("accounts/change-email/", account_views.change_email, name="change_email"),
    path("accounts/password-reset/", account_views.AccountPasswordResetView.as_view(), name="password_reset"),
    path("accounts/password-reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path(
        "accounts/reset/<uidb64>/<token>/", account_views.AccountPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("accounts/password-change/", account_views.AccountPasswordChangeView.as_view(), name="password_change"),
    path("accounts/password-change/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("api/v1/", api.urls),
]
