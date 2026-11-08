from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.access.models import Role, RoleAssignment, RolePermission
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization


ACTIVE_WORKSPACE = "active_workspace_id"


class WorkspaceNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="workspace-owner")
        cls.user = users.create_user(username="workspace-member")
        cls.legacy = users.create_user(username="workspace-legacy-manager")
        cls.first = create_organization(user=cls.owner, name="First workspace")
        cls.second = create_organization(user=cls.owner, name="Second workspace")
        cls.foreign = create_organization(user=cls.owner, name="Foreign workspace")
        cls.inactive = create_organization(user=cls.owner, name="Inactive workspace")
        cls.first_membership = OrganizationMembership.objects.create(
            organization=cls.first, user=cls.user
        )
        cls.second_membership = OrganizationMembership.objects.create(
            organization=cls.second, user=cls.user
        )
        OrganizationMembership.objects.create(
            organization=cls.inactive, user=cls.user, is_active=False
        )
        OrganizationMembership.objects.create(
            organization=cls.first, user=cls.legacy, can_manage_members=True
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.switch_url = reverse("organization-switch")
        self.organization_list = reverse("organization-list")

    def url(self, route, organization):
        return reverse(route, kwargs={"org_id": organization.pk})

    def csrf_headers(self):
        self.client.get(reverse("login"))
        return {"X-CSRFToken": self.client.cookies[settings.CSRF_COOKIE_NAME].value}

    def switch(self, organization, *, csrf=True, **extra):
        return self.client.post(
            self.switch_url,
            {"organization_id": organization.pk, **extra},
            headers=self.csrf_headers() if csrf else {},
        )

    def assert_active(self, organization):
        self.assertEqual(
            self.client.session.get(ACTIVE_WORKSPACE),
            organization.pk if organization else None,
        )

    def grant(self, organization, membership, *permissions):
        role = Role.objects.create(organization=organization, name="Workspace navigation role")
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, code=permission) for permission in permissions]
        )
        RoleAssignment.objects.create(
            organization=organization, role=role, organization_membership=membership
        )

    def assert_link(self, response, url):
        self.assertContains(response, f'href="{url}"')

    def assert_no_link(self, response, url):
        self.assertNotContains(response, f'href="{url}"')

    def test_home_and_organization_list_do_not_choose_membership_automatically(self):
        self.client.force_login(self.user)

        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        self.assert_active(None)
        self.assertEqual(self.client.get(self.organization_list).status_code, 200)
        self.assert_active(None)

    def test_switch_requires_post_and_csrf_and_selects_each_active_membership(self):
        anonymous = self.switch(self.first)
        self.assertRedirects(
            anonymous, f"{reverse('login')}?next={self.switch_url}",
            fetch_redirect_response=False,
        )
        self.assert_active(None)

        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(self.switch_url, {"organization_id": self.first.pk}).status_code, 405
        )
        self.assertEqual(self.switch(self.first, csrf=False).status_code, 403)
        self.assert_active(None)

        for organization in (self.first, self.second):
            with self.subTest(organization=organization.pk):
                response = self.switch(organization, next="https://example.invalid/")
                self.assertRedirects(
                    response, self.url("organization-detail", organization),
                    fetch_redirect_response=False,
                )
                self.assert_active(organization)
        self.assertRedirects(
            self.client.get(reverse("home")), self.url("organization-detail", self.second),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(self.organization_list).status_code, 200)
        self.assert_active(self.second)

    def test_foreign_or_inactive_switch_does_not_replace_current_workspace(self):
        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)

        for unavailable in (self.foreign, self.inactive):
            with self.subTest(organization=unavailable.pk):
                self.assertEqual(self.switch(unavailable).status_code, 404)
                self.assert_active(self.first)

    def test_authorized_direct_path_selects_its_org_even_when_another_is_active(self):
        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)
        response = self.client.get(self.url("organization-departments", self.second))
        self.assertEqual(response.status_code, 200)
        self.assert_active(self.second)

        response = self.client.get(self.url("organization-detail", self.foreign))
        self.assertEqual(response.status_code, 404)
        self.assert_active(self.second)

    def test_successful_api_reads_do_not_change_html_workspace_context(self):
        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)

        response = self.client.get(f"/api/v1/organizations/{self.second.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assert_active(self.first)

    def test_denied_page_in_a_membership_does_not_select_the_workspace(self):
        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)
        response = self.client.get(self.url("organization-projects", self.second))
        self.assertEqual(response.status_code, 404)
        self.assert_active(self.first)

    def test_revoked_selection_is_cleared_without_falling_back_to_another_membership(self):
        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)
        OrganizationMembership.objects.filter(pk=self.first_membership.pk).update(is_active=False)

        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        self.assert_active(None)
        self.assertNotContains(home, self.first.name)
        self.assertEqual(self.client.get(self.organization_list).status_code, 200)
        self.assert_active(None)

        self.assertEqual(self.client.get(self.url("organization-detail", self.second)).status_code, 200)
        self.assert_active(self.second)

    def test_session_is_context_only_and_cannot_bypass_membership_or_permissions(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[ACTIVE_WORKSPACE] = self.foreign.pk
        session.save()
        response = self.client.get(self.organization_list)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.foreign.name)
        self.assert_active(None)

        self.assertEqual(self.switch(self.first).status_code, 302)
        self.assertEqual(self.client.get(self.url("organization-settings", self.first)).status_code, 403)
        self.assertEqual(self.client.get(self.url("organization-projects", self.first)).status_code, 404)
        self.assertEqual(self.client.get(self.url("organization-manage", self.first)).status_code, 403)
        self.assertEqual(self.client.get(self.url("organization-members", self.first)).status_code, 403)
        self.assert_active(self.first)

    def test_links_are_scoped_to_workspace_and_permissions(self):
        self.grant(self.first, self.first_membership, "projects.view", "roles.read")
        self.client.force_login(self.user)
        first_url = self.url("organization-detail", self.first)
        response = self.client.get(first_url)

        self.assert_link(response, self.organization_list)
        self.assert_link(response, self.url("organization-projects", self.first))
        self.assert_link(response, self.url("organization-access", self.first))
        for route in ("organization-settings", "organization-members", "organization-manage"):
            self.assert_no_link(response, self.url(route, self.first))
        self.assert_no_link(response, self.url("organization-projects", self.second))
        self.assert_no_link(response, self.url("organization-access", self.second))
        self.assertNotContains(response, self.foreign.name)

        response = self.client.get(self.url("organization-detail", self.second))
        self.assert_no_link(response, self.url("organization-projects", self.first))
        self.assert_no_link(response, self.url("organization-access", self.first))
        self.assert_no_link(response, self.url("organization-projects", self.second))
        self.assert_link(response, self.url("organization-access", self.second))
        self.assertFalse(
            self.client.get(self.url("organization-access", self.second)).context["can_manage_roles"]
        )

    def test_members_and_settings_links_follow_organization_policies(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url("organization-detail", self.first))
        self.assert_link(response, self.url("organization-settings", self.first))
        self.assert_link(response, self.url("organization-members", self.first))
        self.assert_link(response, self.url("organization-manage", self.first))

        self.client.force_login(self.legacy)
        response = self.client.get(self.url("organization-detail", self.first))
        self.assert_link(response, self.url("organization-members", self.first))
        self.assert_link(response, self.url("organization-manage", self.first))
        self.assert_no_link(response, self.url("organization-settings", self.first))

        Organization.objects.filter(pk=self.first.pk).update(
            department_directory_visibility=Organization.DepartmentDirectoryVisibility.ORGANIZATION
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url("organization-detail", self.first))
        members_url = self.url("organization-members", self.first)
        self.assert_link(response, members_url)
        self.assert_no_link(response, self.url("organization-settings", self.first))
        self.assert_no_link(response, self.url("organization-manage", self.first))
        directory = self.client.get(members_url)
        self.assertEqual(directory.status_code, 200)
        self.assertContains(directory, self.owner.username)

    def test_profile_and_global_settings_require_login_and_remain_global(self):
        paths = [reverse("user-profile"), reverse("global-settings")]
        for path in paths:
            self.assertRedirects(
                self.client.get(path), f"{reverse('login')}?next={path}",
                fetch_redirect_response=False,
            )

        self.client.force_login(self.user)
        self.assertEqual(self.switch(self.first).status_code, 302)
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assert_active(self.first)

    def test_organization_settings_only_changes_target_policy_for_owner_with_csrf(self):
        url = self.url("organization-settings", self.first)
        self.client.force_login(self.legacy)
        self.assertEqual(
            self.client.post(url, {"policy": "both"}, headers=self.csrf_headers()).status_code,
            403,
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, {"policy": "both"}).status_code, 403)
        self.first.refresh_from_db()
        self.assertEqual(self.first.join_policy, Organization.JoinPolicy.INVITES)

        response = self.client.post(
            url, {"policy": "both", "organization_id": self.second.pk},
            headers=self.csrf_headers(),
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.join_policy, Organization.JoinPolicy.BOTH)
        self.assertEqual(self.second.join_policy, Organization.JoinPolicy.INVITES)
