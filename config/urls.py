from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path
from django.views.generic import TemplateView

from apps.organizations.views import organization_detail, organization_list

from .api import api

urlpatterns = [
    path("", login_required(TemplateView.as_view(template_name="home.html")), name="home"),
    path("organizations/", organization_list, name="organization-list"),
    path("organizations/<int:org_id>/", organization_detail, name="organization-detail"),
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/v1/", api.urls),
]
