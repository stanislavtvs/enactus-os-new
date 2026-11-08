from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.join_links import make_join_code
from apps.organizations.models import (
    JoinRequest,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from apps.organizations.onboarding import (
    OnboardingError,
    can_manage_members,
    issue_invitation,
    respond_to_invitation,
    review_join_request,
    revoke_invitation,
    set_join_policy,
    set_member_manager,
    submit_join_request,
)
from apps.organizations.services import create_organization


class OrganizationOnboardingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="onboarding-owner")
        cls.other_owner = users.create_superuser(username="onboarding-other-owner")
        cls.member = users.create_user(username="onboarding-member")
        cls.recipient = users.create_user(username="onboarding-recipient")
        cls.applicant = users.create_user(username="onboarding-applicant")
        cls.org = create_organization(user=cls.owner, name="Onboarding organization")
        cls.other_org = create_organization(
            user=cls.other_owner, name="Foreign onboarding organization"
        )
        cls.membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.member
        )

    def change_policy(self, policy):
        set_join_policy(actor=self.owner, org_id=self.org.pk, policy=policy)

    def invite_recipient(self):
        return issue_invitation(
            actor=self.owner, org_id=self.org.pk, username=self.recipient.username
        )

    def assert_no_membership(self, user):
        self.assertFalse(
            OrganizationMembership.objects.filter(organization=self.org, user=user).exists()
        )

    def test_only_owner_and_active_delegated_member_can_manage_members(self):
        self.assertTrue(can_manage_members(self.owner, self.org))
        self.assertFalse(can_manage_members(self.member, self.org))
        self.assertFalse(can_manage_members(self.other_owner, self.org))

        set_member_manager(
            actor=self.owner,
            org_id=self.org.pk,
            membership_id=self.membership.pk,
            enabled=True,
        )
        self.assertTrue(can_manage_members(self.member, self.org))
        invitation, _ = issue_invitation(
            actor=self.member, org_id=self.org.pk, username=self.recipient.username
        )
        self.assertEqual(invitation.invited_by_id, self.member.pk)

        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        self.assertFalse(can_manage_members(self.member, self.org))
        with self.assertRaises(Http404):
            revoke_invitation(
                actor=self.member, org_id=self.org.pk, invitation_id=invitation.pk
            )

    def test_delegated_manager_cannot_grant_management_or_change_policy(self):
        set_member_manager(
            actor=self.owner,
            org_id=self.org.pk,
            membership_id=self.membership.pk,
            enabled=True,
        )

        with self.assertRaises(PermissionDenied):
            set_member_manager(
                actor=self.member,
                org_id=self.org.pk,
                membership_id=self.membership.pk,
                enabled=False,
            )
        with self.assertRaises(PermissionDenied):
            set_join_policy(
                actor=self.member,
                org_id=self.org.pk,
                policy=Organization.JoinPolicy.REQUESTS,
            )

    def test_invitation_token_can_only_be_used_by_recipient_once(self):
        self.change_policy(Organization.JoinPolicy.INVITES)
        invitation, token = self.invite_recipient()

        with self.assertRaises(Http404):
            respond_to_invitation(actor=self.applicant, token=token, accept=True)
        with self.assertRaises(Http404):
            respond_to_invitation(
                actor=self.applicant, invitation_id=invitation.pk, accept=True
            )
        self.assert_no_membership(self.applicant)

        respond_to_invitation(actor=self.recipient, token=token, accept=True)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.ACCEPTED)
        self.assertTrue(
            OrganizationMembership.objects.get(
                organization=self.org, user=self.recipient
            ).is_active
        )
        with self.assertRaises(OnboardingError):
            respond_to_invitation(actor=self.recipient, token=token, accept=True)

    def test_invitation_decline_does_not_create_membership(self):
        self.change_policy(Organization.JoinPolicy.INVITES)
        invitation, _ = self.invite_recipient()

        respond_to_invitation(
            actor=self.recipient, invitation_id=invitation.pk, accept=False
        )

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.DECLINED)
        self.assert_no_membership(self.recipient)

    def test_only_one_pending_invitation_is_allowed_per_recipient_and_org(self):
        invitation, token = self.invite_recipient()
        self.assertNotEqual(invitation.token_digest, token)
        with self.assertRaises(OnboardingError):
            self.invite_recipient()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationInvitation.objects.create(
                organization=self.org,
                recipient=self.recipient,
                invited_by=self.owner,
                token_digest="f" * 64,
                expires_at=invitation.expires_at,
            )

    def test_invitation_membership_rolls_back_when_resolution_fails(self):
        invitation, token = self.invite_recipient()

        with patch(
            "apps.organizations.onboarding._resolve_invitation",
            side_effect=RuntimeError("invitation resolution failed"),
        ):
            with self.assertRaises(RuntimeError):
                respond_to_invitation(actor=self.recipient, token=token, accept=True)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.PENDING)
        self.assertIsNone(invitation.resolved_at)
        self.assert_no_membership(self.recipient)

    def test_foreign_invitation_cannot_be_revoked_by_another_org_manager(self):
        self.change_policy(Organization.JoinPolicy.INVITES)
        invitation, token = self.invite_recipient()

        with self.assertRaises(Http404):
            revoke_invitation(
                actor=self.other_owner,
                org_id=self.other_org.pk,
                invitation_id=invitation.pk,
            )

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.PENDING)
        revoke_invitation(actor=self.owner, org_id=self.org.pk, invitation_id=invitation.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrganizationInvitation.Status.REVOKED)
        with self.assertRaises(OnboardingError):
            respond_to_invitation(actor=self.recipient, token=token, accept=True)
        self.assert_no_membership(self.recipient)

    def test_invitation_expiry_prevents_membership(self):
        self.change_policy(Organization.JoinPolicy.INVITES)
        invitation, token = self.invite_recipient()
        OrganizationInvitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        with self.assertRaises(OnboardingError):
            respond_to_invitation(actor=self.recipient, token=token, accept=True)
        self.assert_no_membership(self.recipient)

    def test_policy_blocks_new_invitations_and_requests_as_configured(self):
        self.change_policy(Organization.JoinPolicy.REQUESTS)
        with self.assertRaises(OnboardingError):
            self.invite_recipient()

        self.change_policy(Organization.JoinPolicy.INVITES)
        with self.assertRaises(OnboardingError):
            submit_join_request(actor=self.applicant, org_id=self.org.pk)

        self.assertFalse(OrganizationInvitation.objects.filter(organization=self.org).exists())
        self.assertFalse(JoinRequest.objects.filter(organization=self.org).exists())

    def test_request_duplicate_and_cross_org_review_are_blocked(self):
        self.change_policy(Organization.JoinPolicy.REQUESTS)
        request = submit_join_request(actor=self.applicant, org_id=self.org.pk)

        with self.assertRaises(OnboardingError):
            submit_join_request(actor=self.applicant, org_id=self.org.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            JoinRequest.objects.create(organization=self.org, applicant=self.applicant)
        with self.assertRaises(Http404):
            review_join_request(
                actor=self.other_owner,
                org_id=self.other_org.pk,
                request_id=request.pk,
                approve=True,
            )

        request.refresh_from_db()
        self.assertEqual(request.status, JoinRequest.Status.PENDING)
        self.assert_no_membership(self.applicant)

        review_join_request(
            actor=self.owner, org_id=self.org.pk, request_id=request.pk, approve=True
        )

        request.refresh_from_db()
        self.assertEqual(request.status, JoinRequest.Status.APPROVED)
        self.assertTrue(
            OrganizationMembership.objects.get(
                organization=self.org, user=self.applicant
            ).is_active
        )

    def test_request_rejection_does_not_create_membership(self):
        self.change_policy(Organization.JoinPolicy.REQUESTS)
        request = submit_join_request(actor=self.applicant, org_id=self.org.pk)

        review_join_request(
            actor=self.owner, org_id=self.org.pk, request_id=request.pk, approve=False
        )

        request.refresh_from_db()
        self.assertEqual(request.status, JoinRequest.Status.REJECTED)
        self.assert_no_membership(self.applicant)

    def test_join_membership_rolls_back_when_review_save_fails(self):
        self.change_policy(Organization.JoinPolicy.BOTH)
        request = submit_join_request(actor=self.applicant, org_id=self.org.pk)

        with patch.object(JoinRequest, "save", side_effect=RuntimeError("review save failed")):
            with self.assertRaises(RuntimeError):
                review_join_request(
                    actor=self.owner, org_id=self.org.pk, request_id=request.pk, approve=True
                )

        request.refresh_from_db()
        self.assertEqual(request.status, JoinRequest.Status.PENDING)
        self.assertIsNone(request.reviewed_at)
        self.assert_no_membership(self.applicant)

    def test_manage_requires_authorization_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        url = reverse("organization-manage", kwargs={"org_id": self.org.pk})

        self.assertEqual(client.get(url).status_code, 302)
        client.force_login(self.other_owner)
        self.assertIn(client.get(url).status_code, {403, 404})
        client.force_login(self.owner)
        self.assertEqual(client.get(url).status_code, 200)
        response = client.post(url, {"action": "invite", "username": self.recipient.username})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrganizationInvitation.objects.filter(organization=self.org).exists())

    def test_manage_exposes_invitation_token_once_in_the_link_fragment(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        manage_url = reverse("organization-manage", kwargs={"org_id": self.org.pk})
        inbox_url = reverse("organization-invitations")
        client.get(reverse("login"))
        csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        token = "t" * 43

        with patch(
            "apps.organizations.onboarding.secrets.token_urlsafe", return_value=token
        ):
            response = client.post(
                manage_url,
                {"action": "invite", "username": self.recipient.username},
                headers={"X-CSRFToken": csrf},
            )

        self.assertContains(response, f'{inbox_url}#{token}"')
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotContains(client.get(manage_url), token)

        client.force_login(self.recipient)
        client.get(inbox_url)
        csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(
            inbox_url,
            {"decision": "accept", "token": token},
            headers={"X-CSRFToken": csrf},
        )
        self.assertRedirects(response, inbox_url, fetch_redirect_response=False)
        self.assertTrue(
            OrganizationMembership.objects.get(organization=self.org, user=self.recipient).is_active
        )

    def test_inbox_hides_foreign_invitation_and_protects_response_with_csrf(self):
        invitation, _ = self.invite_recipient()
        client = Client(enforce_csrf_checks=True)
        url = reverse("organization-invitations")
        data = {"invitation_id": invitation.pk, "decision": "accept"}

        self.assertEqual(client.get(url).status_code, 302)
        client.force_login(self.applicant)
        self.assertNotContains(client.get(url), self.org.name)
        client.get(reverse("login"))
        csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(client.post(url, data, headers={"X-CSRFToken": csrf}).status_code, 404)

        client.force_login(self.recipient)
        self.assertContains(client.get(url), self.org.name)
        self.assertEqual(client.post(url, data).status_code, 403)
        self.assert_no_membership(self.recipient)

        csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(url, data, headers={"X-CSRFToken": csrf})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertTrue(
            OrganizationMembership.objects.get(organization=self.org, user=self.recipient).is_active
        )

    def test_join_link_requires_login_valid_signature_and_csrf(self):
        self.change_policy(Organization.JoinPolicy.REQUESTS)
        code = make_join_code(self.org.pk)
        url = reverse("organization-join", kwargs={"code": code})
        invalid_url = reverse("organization-join", kwargs={"code": f"{code}x"})
        client = Client(enforce_csrf_checks=True)

        self.assertEqual(client.get(url).status_code, 302)
        client.force_login(self.applicant)
        self.assertEqual(client.get(invalid_url).status_code, 404)
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.post(url).status_code, 403)
        self.assertFalse(JoinRequest.objects.filter(organization=self.org).exists())

        csrf_token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(url, headers={"X-CSRFToken": csrf_token})

        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertTrue(JoinRequest.objects.filter(organization=self.org).exists())
        self.assert_no_membership(self.applicant)
        self.change_policy(Organization.JoinPolicy.INVITES)
        self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(client.post(url, headers={"X-CSRFToken": csrf_token}).status_code, 404)
