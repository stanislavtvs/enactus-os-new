from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path
from django.views.generic import TemplateView

from apps.departments.views import department_invitations, department_members, department_structure
from apps.organizations.onboarding_views import (
    organization_invitations,
    organization_join,
    organization_manage,
)
from apps.organizations.views import organization_detail, organization_list

from .api import api

urlpatterns = [
    path("", login_required(TemplateView.as_view(template_name="home.html")), name="home"),
    path("organizations/", organization_list, name="organization-list"),
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
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/v1/", api.urls),
]
