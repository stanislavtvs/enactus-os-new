import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization

API = "/api/v1/organizations/"


class OnboardingAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="api-owner")
        cls.invitee = users.create_user(username="api-invitee")
        cls.outsider = users.create_user(username="api-outsider")
        cls.organization = create_organization(user=cls.owner, name="API organization")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_api = f"{API}{self.organization.pk}/"

    def authenticate(self, user):
        self.client.force_login(user)
        self.client.get("/accounts/login/")
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def send(self, method, path, data=None):
        return self.client.generic(
            method, path, data="" if data is None else json.dumps(data),
            content_type="application/json", headers={"X-CSRFToken": self.csrf},
        )

    def test_invitation_token_is_one_time_and_only_visible_at_issue(self):
        self.authenticate(self.owner)
        url = self.org_api + "invitations/"
        self.assertEqual(self.client.post(url, {"username": self.invitee.username},
                         content_type="application/json").status_code, 403)
        issued = self.send("POST", url, {"username": self.invitee.username})
        self.assertEqual(issued.status_code, 201, issued.content)
        self.assertEqual(issued["Cache-Control"], "no-store")
        token = issued.json()["token"]

        managed = self.client.get(self.org_api + "members/")
        self.assertEqual(managed.status_code, 200)
        self.assertNotContains(managed, token)

        self.authenticate(self.outsider)
        self.assertEqual(self.client.get(self.org_api + "members/").status_code, 404)
        self.assertEqual(self.client.get(API + "invitations/").json(), [])
        decision = {"token": token, "decision": "accept"}
        self.assertEqual(self.send("POST", API + "invitations/respond/", decision).status_code, 404)

        self.authenticate(self.invitee)
        inbox = self.client.get(API + "invitations/")
        self.assertEqual(inbox.json()[0]["organization_name"], self.organization.name)
        self.assertNotContains(inbox, token)
        self.assertEqual(self.send("POST", API + "invitations/respond/", decision).status_code, 200)
        self.assertEqual(self.send("POST", API + "invitations/respond/", decision).status_code, 409)
        membership = OrganizationMembership.objects.get(organization=self.organization, user=self.invitee)
        self.assertFalse(membership.can_manage_members)

    def test_signed_join_and_owner_review(self):
        self.authenticate(self.owner)
        self.assertEqual(self.send("PATCH", self.org_api + "join-policy/", {"policy": "both"}).status_code, 204)
        code = self.client.get(self.org_api + "members/").json()["join_code"]

        self.authenticate(self.invitee)
        url = API + f"join/{code}/"
        self.assertEqual(self.client.post(url).status_code, 403)
        submitted = self.send("POST", url)
        self.assertEqual(submitted.status_code, 201)
        self.assertEqual(self.send("POST", url).status_code, 409)

        review = self.org_api + f"join-requests/{submitted.json()['id']}/review/"
        self.assertEqual(self.send("POST", review, {"approve": True}).status_code, 404)
        self.authenticate(self.owner)
        approved = self.send("POST", review, {"approve": True})
        self.assertEqual(approved.json()["status"], "approved")
        self.assertTrue(OrganizationMembership.objects.filter(
            organization=self.organization, user=self.invitee, is_active=True,
        ).exists())

    def test_api_rejects_null_character_in_recipient_name(self):
        self.authenticate(self.owner)
        response = self.send("POST", self.org_api + "invitations/", {"username": "a\0b"})
        self.assertEqual(response.status_code, 422)

    def test_invitation_limit_applies_to_new_invites_by_the_same_manager(self):
        self.authenticate(self.owner)
        url = self.org_api + "invitations/"
        with patch("apps.organizations.onboarding.INVITATIONS_PER_HOUR", 1):
            self.assertEqual(self.send("POST", url, {"username": self.invitee.username}).status_code, 201)
            self.assertEqual(self.send("POST", url, {"username": self.outsider.username}).status_code, 429)

    def test_request_limit_applies_even_after_a_rejection(self):
        self.authenticate(self.owner)
        self.assertEqual(self.send("PATCH", self.org_api + "join-policy/", {"policy": "both"}).status_code, 204)
        code = self.client.get(self.org_api + "members/").json()["join_code"]
        url = API + f"join/{code}/"
        self.authenticate(self.invitee)
        submitted = self.send("POST", url)
        self.assertEqual(submitted.status_code, 201)
        review = self.org_api + f"join-requests/{submitted.json()['id']}/review/"
        self.authenticate(self.owner)
        self.assertEqual(self.send("POST", review, {"approve": False}).status_code, 200)
        self.authenticate(self.invitee)
        with patch("apps.organizations.onboarding.REQUESTS_PER_HOUR", 1):
            self.assertEqual(self.send("POST", url).status_code, 429)
