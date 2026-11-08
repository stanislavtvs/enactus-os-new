from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.departments.models import Department
from apps.notifications.models import Notification
from apps.organizations.models import Organization, OrganizationMembership, OwnershipTransfer
from apps.organizations.ownership import respond_to_offer
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


BASE = "/api/v1/organizations/"
INBOX = f"{BASE}ownership-transfers/"


def transfer_url(organization):
    return f"{BASE}{organization.pk}/ownership-transfer/"


def reply(client, transfer_id, accept=True, *, csrf=True):
    return write(client, "post", f"{INBOX}{transfer_id}/respond/", {"accept": accept}, csrf=csrf)


class OwnershipTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="ownership-owner")
        cls.foreign_owner = users.create_superuser(username="ownership-foreign-owner")
        cls.recipient = users.create_user(username="ownership-recipient")
        cls.delegate = users.create_user(username="ownership-delegate")
        cls.other = users.create_user(username="ownership-other-member")
        cls.inactive = users.create_user(username="ownership-inactive")
        cls.org = create_organization(user=cls.owner, name="Ownership workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign ownership workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)

        def member(user, **fields):
            return OrganizationMembership.objects.create(organization=cls.org, user=user, **fields)

        cls.recipient_member = member(cls.recipient)
        cls.delegate_member = member(cls.delegate, can_manage_members=True)
        cls.other_member = member(cls.other)
        cls.inactive_member = member(cls.inactive, is_active=False)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.department = Department.objects.create(organization=cls.org, name="Ownership department")
        cls.project = Project.objects.create(
            organization=cls.org, created_by=cls.owner, name="Ownership project",
            lead_membership=cls.owner_member, responsible_department=cls.department,
        )
        ProjectMembership.objects.create(
            organization=cls.org, project=cls.project, organization_membership=cls.recipient_member,
        )
        cls.task = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, name="Ownership task",
            project=cls.project, department=cls.department, assignee=cls.recipient_member,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = transfer_url(self.org)

    def offer(self, recipient_id=None, *, csrf=True, confirm=True):
        return write(self.client, "post", self.url, {
            "recipient_id": recipient_id if recipient_id is not None else self.recipient_member.pk,
            "confirm": confirm,
        }, csrf=csrf)

    def make_offer(self):
        self.client.force_login(self.owner)
        response = self.offer()
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def assert_owner(self, user):
        self.org.refresh_from_db()
        self.assertEqual(self.org.owner_id, user.pk)
        self.assertTrue(self.org.memberships.get(user=user).is_active)

    def test_acceptance_swaps_owner_but_preserves_memberships_explicit_roles_and_objects(self):
        assignment = grant(self.org, self.owner_member, "projects.view", "tasks.view")
        previous_audit = AuditEvent.objects.get(organization=self.org, action="organization.created")
        self.client.force_login(self.owner)
        policy = write(self.client, "patch", f"{BASE}{self.org.pk}/notification-policy/", {"enabled_categories": []})
        self.assertEqual(policy.status_code, 200, policy.content)
        self.client.force_login(self.recipient)
        preference = write(
            self.client, "patch", f"{BASE}{self.org.pk}/notification-preferences/", {"enabled_categories": []},
        )
        self.assertEqual(preference.status_code, 200, preference.content)
        offered = self.make_offer()
        transfer_id = offered["id"]
        self.assertEqual(offered["status"], "pending")
        self.assert_owner(self.owner)
        notice = Notification.objects.get(
            event__kind="organization.ownership_offered", event__object_id=transfer_id,
            recipient=self.recipient,
        )
        self.assertFalse(Notification.objects.filter(
            event__kind="organization.ownership_offered", event__object_id=transfer_id,
            recipient=self.delegate,
        ).exists())

        self.client.force_login(self.recipient)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        link = self.client.get(f"/api/v1/notifications/{notice.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertEqual(link.json()["url"], reverse("ownership-inbox"))
        self.assert_owner(self.owner)
        inbox = self.client.get(INBOX)
        self.assertEqual(inbox.status_code, 200, inbox.content)
        self.assertEqual([row["id"] for row in inbox.json()], [transfer_id])
        accepted = reply(self.client, transfer_id)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(accepted.json()["status"], "accepted")
        self.assert_owner(self.recipient)
        self.assertEqual(reply(self.client, transfer_id).status_code, 409)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(f"{BASE}{self.org.pk}/audit/").status_code, 200)

        self.owner_member.refresh_from_db()
        assignment.refresh_from_db()
        self.project.refresh_from_db()
        self.task.refresh_from_db()
        previous_audit.refresh_from_db()
        self.assertTrue(self.owner_member.is_active)
        self.assertIsNone(assignment.revoked_at)
        self.assertEqual(self.project.lead_membership_id, self.owner_member.pk)
        self.assertEqual(self.task.created_by_id, self.owner_member.pk)
        self.assertEqual(self.task.department_id, self.department.pk)
        self.assertEqual(self.task.assignee_id, self.recipient_member.pk)
        self.assertEqual(previous_audit.actor_id, self.owner.pk)
        completed = AuditEvent.objects.get(organization=self.org, action="organization.ownership_transferred")
        self.assertEqual(completed.actor_id, self.recipient.pk)
        self.assertEqual(completed.changes["owner_id"], {"old": self.owner.pk, "new": self.recipient.pk})
        self.assertCountEqual(Notification.objects.filter(
            event__kind="organization.ownership_accepted", event__organization=self.org,
        ).values_list("recipient_id", flat=True), [self.owner.pk, self.recipient.pk])

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(f"{BASE}{self.org.pk}/notification-policy/").status_code, 403)
        self.assertEqual(self.client.get(f"{BASE}{self.org.pk}/tasks/{self.task.pk}/").status_code, 200)

    def test_only_current_owner_can_offer_and_only_recipient_can_respond_with_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.get(INBOX).status_code, 401)
        self.client.force_login(self.delegate)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.offer().status_code, 403)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.offer().status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.offer(csrf=False).status_code, 403)
        self.assertEqual(self.offer(confirm=False).status_code, 422)
        self.assertEqual(self.offer(self.owner_member.pk).status_code, 409)
        self.assertEqual(self.offer(self.foreign_member.pk).status_code, 404)
        self.assertFalse(OwnershipTransfer.objects.filter(organization=self.org).exists())

        offered = self.make_offer()
        transfer_id = offered["id"]
        self.assertEqual(self.offer(self.other_member.pk).status_code, 409)
        for actor in (self.owner, self.other, self.foreign_owner):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                self.assertEqual(self.client.get(INBOX).json(), [])
                self.assertEqual(reply(self.client, transfer_id).status_code, 404)
        self.client.force_login(self.recipient)
        self.assertEqual(reply(self.client, transfer_id, csrf=False).status_code, 403)
        self.assertEqual(reply(self.client, transfer_id).status_code, 200)
        self.assert_owner(self.recipient)

    def test_candidates_exclude_self_foreign_and_inactive_members(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["pending"])
        candidates = {row["id"] for row in response.json()["candidates"]}
        self.assertEqual(candidates, {self.recipient_member.pk, self.delegate_member.pk, self.other_member.pk})
        denied = self.offer(self.inactive_member.pk)
        self.assertEqual(denied.status_code, 404, denied.content)
        self.other.is_active = False
        self.other.save(update_fields=["is_active"])
        refreshed = self.client.get(self.url).json()
        self.assertNotIn(self.other_member.pk, [row["id"] for row in refreshed["candidates"]])
        self.assertEqual(self.offer(self.other_member.pk).status_code, 404)
        self.assertFalse(OwnershipTransfer.objects.filter(organization=self.org).exists())

    def test_revoke_and_decline_are_terminal_and_leave_owner_unchanged(self):
        first = self.make_offer()
        revoke_url = f"{self.url}{first['id']}/revoke/"
        self.assertEqual(write(self.client, "post", revoke_url, csrf=False).status_code, 403)
        self.client.force_login(self.delegate)
        self.assertEqual(write(self.client, "post", revoke_url).status_code, 403)
        self.client.force_login(self.owner)
        revoked = write(self.client, "post", revoke_url)
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertEqual(revoked.json()["status"], "revoked")
        self.client.force_login(self.recipient)
        self.assertEqual(reply(self.client, first["id"]).status_code, 409)

        second = self.make_offer()
        self.client.force_login(self.recipient)
        declined = reply(self.client, second["id"], accept=False)
        self.assertEqual(declined.status_code, 200, declined.content)
        self.assertEqual(declined.json()["status"], "declined")
        self.assertEqual(reply(self.client, second["id"]).status_code, 409)
        self.assert_owner(self.owner)
        self.assertTrue(AuditEvent.objects.filter(
            organization=self.org, action="ownership_transfer.revoked", object_id=first["id"],
        ).exists())
        self.assertTrue(AuditEvent.objects.filter(
            organization=self.org, action="ownership_transfer.declined", object_id=second["id"],
        ).exists())

    def test_expired_offer_cannot_be_accepted_and_replacement_preserves_history(self):
        offered = self.make_offer()
        transfer_id = offered["id"]
        expires_at = OwnershipTransfer.objects.get(pk=transfer_id).expires_at
        created_at = datetime.fromisoformat(offered["created_at"])
        self.assertGreater(expires_at - created_at, timedelta(days=6))
        self.assertLessEqual(expires_at - created_at, timedelta(days=7))
        with patch("apps.organizations.ownership.timezone") as clock:
            clock.now.return_value = expires_at
            self.client.force_login(self.recipient)
            self.assertEqual(self.client.get(INBOX).json()[0]["status"], "expired")
            self.assertEqual(reply(self.client, transfer_id).status_code, 409)
            self.client.force_login(self.owner)
            self.assertIsNone(self.client.get(self.url).json()["pending"])
            replacement = self.offer(self.other_member.pk)
        self.assertEqual(replacement.status_code, 201, replacement.content)
        expired = OwnershipTransfer.objects.get(pk=transfer_id)
        self.assertEqual(expired.status, OwnershipTransfer.Status.EXPIRED)
        self.assertIsNotNone(expired.resolved_at)
        self.assertTrue(AuditEvent.objects.filter(
            organization=self.org, action="ownership_transfer.expired", object_id=transfer_id,
        ).exists())
        self.assert_owner(self.owner)

    def test_recipient_deactivation_invalidates_offer_even_after_reactivation(self):
        offered = self.make_offer()
        transfer_id = offered["id"]
        OrganizationMembership.objects.filter(pk=self.recipient_member.pk).update(is_active=False)
        self.client.force_login(self.recipient)
        self.assertEqual(self.client.get(INBOX).json(), [])
        self.assertEqual(reply(self.client, transfer_id).status_code, 404)
        OrganizationMembership.objects.filter(pk=self.recipient_member.pk).update(is_active=True)
        self.assertEqual(reply(self.client, transfer_id).status_code, 409)
        self.assert_owner(self.owner)

    def test_pending_offer_cannot_survive_a_change_of_current_owner(self):
        offered = self.make_offer()
        transfer_id = offered["id"]
        Organization.objects.filter(pk=self.org.pk).update(owner=self.other)
        self.assert_owner(self.other)
        self.client.force_login(self.recipient)
        self.assertEqual(reply(self.client, transfer_id).status_code, 409)
        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "post", f"{self.url}{transfer_id}/revoke/").status_code, 403)
        self.client.force_login(self.other)
        replacement = self.offer()
        self.assertEqual(replacement.status_code, 201, replacement.content)
        self.assertEqual(OwnershipTransfer.objects.get(pk=transfer_id).status, OwnershipTransfer.Status.REVOKED)

    def test_audit_failure_rolls_back_acceptance_owner_and_delivery(self):
        offered = self.make_offer()
        transfer_id = offered["id"]
        with patch("apps.organizations.ownership.record", side_effect=IntegrityError("mandatory audit failed")):
            with self.assertRaisesRegex(IntegrityError, "mandatory audit failed"):
                respond_to_offer(user=self.recipient, transfer_id=transfer_id, accept=True)
        transfer = OwnershipTransfer.objects.get(pk=transfer_id)
        self.assertEqual(transfer.status, OwnershipTransfer.Status.PENDING)
        self.assertIsNone(transfer.resolved_at)
        self.assert_owner(self.owner)
        self.assertFalse(AuditEvent.objects.filter(
            organization=self.org, action="organization.ownership_transferred",
        ).exists())
        self.assertFalse(Notification.objects.filter(
            event__kind="organization.ownership_accepted", event__organization=self.org,
        ).exists())

    def test_postgresql_rejects_owner_membership_deactivation_and_foreign_offer(self):
        offered = self.make_offer()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.filter(pk=self.owner_member.pk).update(is_active=False)
            with connections["default"].cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OwnershipTransfer.objects.create(
                organization=self.org, from_owner=self.owner_member, recipient=self.foreign_member,
                status=OwnershipTransfer.Status.REVOKED,
                expires_at=timezone.now() + timedelta(days=1), resolved_at=timezone.now(),
            )
            with connections["default"].cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assert_owner(self.owner)
        self.assertEqual(OwnershipTransfer.objects.get(pk=offered["id"]).status, "pending")


class ConcurrentOwnershipTransferTests(TransactionTestCase):
    def test_two_simultaneous_accepts_leave_one_owner_one_audit_and_one_result_per_party(self):
        users = get_user_model().objects
        old_owner = users.create_superuser(username="ownership-race-owner")
        recipient = users.create_user(username="ownership-race-recipient")
        org = create_organization(user=old_owner, name="Concurrent ownership")
        member = OrganizationMembership.objects.create(organization=org, user=recipient)
        owner_client = Client(enforce_csrf_checks=True)
        owner_client.force_login(old_owner)
        offered = write(owner_client, "post", transfer_url(org), {"recipient_id": member.pk, "confirm": True})
        self.assertEqual(offered.status_code, 201, offered.content)
        transfer_id = offered.json()["id"]
        start = Barrier(2, timeout=8)

        def accept():
            connection = connections["default"]
            try:
                client = Client(enforce_csrf_checks=True)
                client.force_login(recipient)
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                start.wait()
                return reply(client, transfer_id).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept) for _ in range(2)]
            statuses = [future.result(timeout=16) for future in futures]
        self.assertEqual(sorted(statuses), [200, 409])
        org.refresh_from_db()
        self.assertEqual(org.owner_id, recipient.pk)
        self.assertCountEqual(org.memberships.filter(is_active=True).values_list("user_id", flat=True), [
            old_owner.pk, recipient.pk,
        ])
        self.assertEqual(OwnershipTransfer.objects.get(pk=transfer_id).status, OwnershipTransfer.Status.ACCEPTED)
        self.assertEqual(AuditEvent.objects.filter(organization=org, action="organization.ownership_transferred").count(), 1)
        self.assertCountEqual(Notification.objects.filter(
            event__organization=org, event__kind="organization.ownership_accepted",
        ).values_list("recipient_id", flat=True), [old_owner.pk, recipient.pk])
