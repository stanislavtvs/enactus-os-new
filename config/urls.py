from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from apps.access.views import organization_access_page
from apps.departments.views import department_invitations, department_members, department_structure
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
    user_profile,
)

from apps.projects.staffing_views import (
    organization_staffing_page,
    project_invitation_page,
    project_team_page,
    staffing_request_page,
)
from apps.projects.views import organization_project, organization_projects

from .api import api

urlpatterns = [
    path("", home, name="home"),
    path("organizations/", organization_list, name="organization-list"),
    path("organizations/switch/", organization_switch, name="organization-switch"),
    path("organizations/<int:org_id>/members/", organization_members, name="organization-members"),
    path("organizations/<int:org_id>/settings/", organization_settings, name="organization-settings"),
    path("accounts/profile/", user_profile, name="user-profile"),
    path("accounts/settings/", global_settings, name="global-settings"),
    path("organizations/<int:org_id>/", organization_detail, name="organization-detail"),
    path("organizations/<int:org_id>/manage/", organization_manage, name="organization-manage"),
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
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/v1/", api.urls),
]
