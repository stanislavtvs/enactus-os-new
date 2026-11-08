from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization


API_LIST = "/api/v1/organizations/"
PAGE_LIST = "/organizations/"


class OrganizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.creator = users.create_user(username="org-creator", is_staff=True)
        cls.creator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="organizations", codename="add_organization"
            )
        )
        cls.member = users.create_user(username="org-member")
        cls.staff_without_permission = users.create_user(
            username="staff-without-org-permission", is_staff=True
        )
        cls.superuser = users.create_superuser(username="org-platform-admin")
        cls.first = create_organization(user=cls.creator, name="First organization")
        cls.second = create_organization(user=cls.creator, name="Second organization")
        cls.inactive = create_organization(user=cls.creator, name="Inactive organization")
        cls.foreign = create_organization(user=cls.creator, name="Private organization")
        OrganizationMembership.objects.bulk_create(
            [
                OrganizationMembership(user=cls.member, organization=cls.first),
                OrganizationMembership(user=cls.member, organization=cls.second),
                OrganizationMembership(
                    user=cls.member, organization=cls.inactive, is_active=False
                ),
            ]
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def post_organization(self, payload, *, csrf=True):
        headers = {}
        if csrf:
            self.client.get("/accounts/login/")
            headers["X-CSRFToken"] = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        return self.client.post(
            API_LIST, payload, content_type="application/json", headers=headers
        )

    def test_creation_uses_the_authenticated_creator_and_adds_active_membership(self):
        self.client.force_login(self.creator)

        response = self.post_organization({"name": "  New organization  "})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(set(body), {"id", "name", "created_at"})
        self.assertEqual(body["name"], "New organization")
        organization = Organization.objects.get(pk=body["id"])
        self.assertEqual(organization.owner_id, self.creator.pk)
        self.assertIsNotNone(organization.created_at)
        membership = OrganizationMembership.objects.get(
            organization=organization, user=self.creator
        )
        self.assertTrue(membership.is_active)
        self.assertIsNotNone(membership.joined_at)
        self.assertEqual(
            self.client.get(f"{API_LIST}{organization.pk}/").json(), body
        )

    def test_creation_rolls_back_if_owner_membership_fails(self):
        with patch.object(
            OrganizationMembership.objects, "create", side_effect=IntegrityError
        ):
            with self.assertRaises(IntegrityError):
                create_organization(user=self.creator, name="Must roll back")

        self.assertFalse(Organization.objects.filter(name="Must roll back").exists())

    def test_duplicate_membership_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(
                user=self.member, organization=self.first, is_active=False
            )

    def test_member_sees_multiple_organizations_but_only_with_active_membership(self):
        self.client.force_login(self.member)

        response = self.client.get(API_LIST)

        self.assertEqual(response.status_code, 200)
        organizations = response.json()
        self.assertEqual(
            {organization["id"] for organization in organizations},
            {self.first.pk, self.second.pk},
        )
        self.assertEqual(len(organizations), 2)
        for organization in organizations:
            self.assertEqual(set(organization), {"id", "name", "created_at"})

    def test_details_hide_foreign_inactive_and_missing_organizations(self):
        self.client.force_login(self.member)

        self.assertEqual(self.client.get(f"{API_LIST}{self.first.pk}/").status_code, 200)
        missing = self.client.get(f"{API_LIST}{self.foreign.pk + 1000}/")
        self.assertEqual(missing.status_code, 404)
        for organization in (self.foreign, self.inactive):
            with self.subTest(organization=organization.pk):
                response = self.client.get(f"{API_LIST}{organization.pk}/")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), missing.json())

    def test_platform_superuser_cannot_read_an_unjoined_organization(self):
        self.client.force_login(self.superuser)

        self.assertEqual(self.client.get(API_LIST).json(), [])
        self.assertEqual(
            self.client.get(f"{API_LIST}{self.foreign.pk}/").status_code, 404
        )

    def test_anonymous_api_requests_require_authentication(self):
        self.assertEqual(self.client.get(API_LIST).status_code, 401)
        self.assertEqual(self.client.get(f"{API_LIST}{self.first.pk}/").status_code, 401)
        self.assertEqual(
            self.post_organization({"name": "Anonymous organization"}).status_code, 401
        )
        self.assertFalse(Organization.objects.filter(name="Anonymous organization").exists())

    def test_creation_requires_permission_in_api_and_service(self):
        for user in (self.member, self.staff_without_permission):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.post_organization({"name": "Forbidden organization"})
                self.assertEqual(response.status_code, 403)
                with self.assertRaises(PermissionDenied):
                    create_organization(user=user, name="Forbidden organization")

        self.assertFalse(Organization.objects.filter(name="Forbidden organization").exists())

    def test_creation_requires_csrf_even_with_permission(self):
        self.client.force_login(self.creator)
        response = self.post_organization({"name": "No CSRF"}, csrf=False)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Organization.objects.filter(name="No CSRF").exists())

    def test_creation_rejects_invalid_names(self):
        self.client.force_login(self.creator)
        before = Organization.objects.count()
        for payload in (
            {},
            {"name": "  "},
            {"name": "x" * 201},
            {"name": None},
            {"name": "x\0y"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_organization(payload).status_code, 422)

        self.assertEqual(Organization.objects.count(), before)

    def test_pages_require_login(self):
        for path in (PAGE_LIST, f"{PAGE_LIST}{self.first.pk}/"):
            with self.subTest(path=path):
                self.assertRedirects(self.client.get(path), f"/accounts/login/?next={path}")

    def test_creation_form_is_shown_only_to_users_with_permission(self):
        self.client.force_login(self.creator)
        self.assertContains(self.client.get(PAGE_LIST), 'id="organization-create"')

        self.client.force_login(self.staff_without_permission)
        self.assertNotContains(self.client.get(PAGE_LIST), 'id="organization-create"')

    def test_pages_share_membership_boundary_and_escape_names(self):
        name = "<script>tenant-test</script>"
        self.first.name = name
        self.first.save(update_fields=["name"])
        self.client.force_login(self.member)

        response = self.client.get(PAGE_LIST)
        self.assertContains(response, "&lt;script&gt;tenant-test&lt;/script&gt;")
        self.assertNotContains(response, name)
        self.assertContains(response, self.second.name)
        self.assertNotContains(response, self.foreign.name)
        self.assertNotContains(response, self.inactive.name)
        detail = self.client.get(f"{PAGE_LIST}{self.first.pk}/")
        self.assertContains(detail, "&lt;script&gt;tenant-test&lt;/script&gt;")
        self.assertNotContains(detail, name)
        for organization in (self.foreign, self.inactive):
            self.assertEqual(
                self.client.get(f"{PAGE_LIST}{organization.pk}/").status_code, 404
            )
