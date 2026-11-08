from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.notifications.services import publish
from apps.organizations.models import Organization
from apps.organizations.platform_models import PlatformAuditEvent
from apps.organizations.services import create_organization
from apps.tasks.models import Task
from tests.test_department_memberships import write


PLATFORM = "/api/v1/platform/"
ORGANIZATIONS = "/api/v1/organizations/"
NOTIFICATIONS = "/api/v1/notifications/"
PLATFORM_GROUP = "enactus-platform-admin"


def account(username, *, verified=True):
    return get_user_model().objects.create_user(
        username=username,
        email=f"{username}@example.invalid" if verified else "",
        email_verified_at=timezone.now() if verified else None,
    )


def platform_admin(username):
    user = account(username)
    group, _ = Group.objects.get_or_create(name=PLATFORM_GROUP)
    user.groups.add(group)
    return user


def session(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client

POLICY = f"{PLATFORM}creation-policy/"
REQUESTS = f"{PLATFORM}creation-requests/"
REVIEW = f"{PLATFORM}admin/creation-requests/"
GRANTS = f"{PLATFORM}creation-grants/"


def set_mode(client, mode):
    return write(client, "patch", POLICY, {"mode": mode})


def submit(client, name="Proposed workspace"):
    return write(client, "post", REQUESTS, {"name": name, "description": "A public purpose"})


def decide(client, request_id, decision="approve", comment="Reviewed"):
    return write(
        client, "post", f"{REVIEW}{request_id}/decision/",
        {"decision": decision, "comment": comment},
    )


class PlatformCreationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = platform_admin("creation-admin")
        cls.alice = account("creation-alice")
        cls.bob = account("creation-bob")
        cls.legacy = account("creation-legacy", verified=False)
        cls.superuser = get_user_model().objects.create_superuser(username="creation-root")
        cls.legacy.user_permissions.add(Permission.objects.get(
            content_type__app_label="organizations", codename="add_organization"
        ))
        cls.existing = create_organization(user=cls.legacy, name="Private existing workspace")

    def setUp(self):
        self.admin_client = session(self.admin)
        self.alice_client = session(self.alice)

    def use_mode(self, mode):
        response = set_mode(self.admin_client, mode)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["mode"], mode)

    def make_request(self, client=None, name="Proposed workspace"):
        response = submit(client or self.alice_client, name)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "pending")
        self.assertIsNone(response.json()["organization_id"])
        return response.json()["id"]

    def assert_workspace(self, organization_id, user):
        organization = Organization.objects.get(pk=organization_id)
        self.assertEqual(organization.owner_id, user.pk)
        self.assertEqual(list(organization.memberships.values_list("user_id", "is_active")), [(user.pk, True)])
        self.assertEqual(organization.disabled_modules, [])
        self.assertTrue(AuditEvent.objects.filter(
            organization=organization, action="organization.created", object_id=organization.pk,
        ).exists())
        return organization

    def test_default_keeps_existing_django_create_permissions_but_separates_platform_admin(self):
        self.assertEqual(self.alice_client.get(POLICY).json()["mode"], "permission_only")
        denied = write(self.alice_client, "post", ORGANIZATIONS, {"name": "Forbidden"})
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertFalse(self.admin_client.get(POLICY).json()["can_create"])
        for user in (self.legacy, self.superuser):
            with self.subTest(user=user.username):
                client = session(user)
                self.assertFalse(client.get(POLICY).json()["can_administer"])
                self.assertEqual(set_mode(client, "open").status_code, 403)
                created = write(client, "post", ORGANIZATIONS, {"name": user.username})
                self.assertEqual(created.status_code, 201, created.content)
                self.assert_workspace(created.json()["id"], user)
        self.assertEqual(self.admin_client.get(f"{ORGANIZATIONS}{self.existing.pk}/").status_code, 404)
        self.assertEqual(self.admin_client.get(ORGANIZATIONS).json(), [])
        self.assertEqual(session(self.legacy).get("/platform/").status_code, 403)
        self.assertEqual(session(self.superuser).get("/platform/").status_code, 403)
        self.assertContains(self.admin_client.get("/platform/"), 'id="platform-app"')

    def test_open_requires_authentication_csrf_and_verified_email(self):
        self.use_mode("open")
        self.assertEqual(Client().get(POLICY).status_code, 401)
        self.assertEqual(write(Client(enforce_csrf_checks=True), "post", ORGANIZATIONS, {"name": "Anonymous"}).status_code, 401)
        self.assertEqual(write(self.alice_client, "post", ORGANIZATIONS, {"name": "No CSRF"}, csrf=False).status_code, 403)
        unverified = session(account("creation-unverified", verified=False))
        self.assertFalse(unverified.get(POLICY).json()["can_create"])
        self.assertEqual(write(unverified, "post", ORGANIZATIONS, {"name": "Unverified"}).status_code, 403)
        created = write(self.alice_client, "post", ORGANIZATIONS, {"name": "Open workspace"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assert_workspace(created.json()["id"], self.alice)

    def test_direct_idempotency_is_per_user_and_reuses_workspace(self):
        self.use_mode("open")
        payload = {"name": "Repeated workspace", "idempotency_key": str(uuid4())}
        first = write(self.alice_client, "post", ORGANIZATIONS, payload)
        self.assertEqual(first.status_code, 201, first.content)
        replay = write(self.alice_client, "post", ORGANIZATIONS, payload)
        self.assertIn(replay.status_code, (200, 201), replay.content)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        mismatched = write(self.alice_client, "post", ORGANIZATIONS, {**payload, "name": "Changed name"})
        self.assertEqual(mismatched.status_code, 409, mismatched.content)
        self.assertFalse(Organization.objects.filter(name="Changed name").exists())
        other = write(session(self.bob), "post", ORGANIZATIONS, payload)
        self.assertEqual(other.status_code, 201, other.content)
        self.assertNotEqual(first.json()["id"], other.json()["id"])
        self.assert_workspace(other.json()["id"], self.bob)
        self.assertEqual(Organization.objects.filter(name=payload["name"]).count(), 2)

    def test_legacy_post_retries_do_not_duplicate_an_organization(self):
        client = session(self.legacy)
        first = write(client, "post", ORGANIZATIONS, {"name": "Legacy replay"})
        self.assertEqual(first.status_code, 201, first.content)
        replay = write(client, "post", ORGANIZATIONS, {"name": " legacy   replay "})
        self.assertIn(replay.status_code, (200, 201), replay.content)
        self.assertEqual(replay.json()["id"], first.json()["id"])

    def test_individual_grant_no_self_or_tenant_escalation_and_revocation(self):
        owner_client = session(self.legacy)
        self.assertEqual(write(owner_client, "post", GRANTS, {"username": self.alice.username}).status_code, 403)
        self.assertEqual(write(self.admin_client, "post", GRANTS, {"username": self.admin.username}).status_code, 403)
        self.assertEqual(write(self.admin_client, "post", GRANTS, {"username": self.alice.username}, csrf=False).status_code, 403)
        response = write(self.admin_client, "post", GRANTS, {"username": self.alice.username})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["user"]["id"], self.alice.pk)
        self.assertEqual(response.json()["granted_by"]["id"], self.admin.pk)
        self.assertTrue(self.alice_client.get(POLICY).json()["can_create"])
        self.assertFalse(session(self.bob).get(POLICY).json()["can_create"])
        created = write(self.alice_client, "post", ORGANIZATIONS, {"name": "Granted workspace"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(write(self.admin_client, "delete", f"{GRANTS}{self.alice.pk}/").status_code, 204)
        self.assertEqual(write(self.alice_client, "post", ORGANIZATIONS, {"name": "Revoked"}).status_code, 403)
        self.assertEqual(self.alice_client.get(f'{ORGANIZATIONS}{created.json()["id"]}/').status_code, 200)

    def test_request_mode_guards_old_api_service_and_request_ownership(self):
        self.use_mode("request_approval")
        self.assertTrue(self.alice_client.get(POLICY).json()["can_request"])
        self.assertEqual(write(self.alice_client, "post", ORGANIZATIONS, {"name": "Bypass"}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            create_organization(user=self.superuser, name="Bypass service")
        self.assertEqual(write(self.alice_client, "post", REQUESTS, {"name": "No CSRF"}, csrf=False).status_code, 403)
        request_id = self.make_request()
        self.assertFalse(Organization.objects.filter(name="Proposed workspace").exists())
        self.assertEqual([row["id"] for row in self.alice_client.get(REQUESTS).json()["items"]], [request_id])
        bob = session(self.bob)
        self.assertEqual(bob.get(REQUESTS).json()["items"], [])
        self.assertEqual(bob.get(f"{REVIEW}{request_id}/").status_code, 403)
        self.assertEqual(write(bob, "post", f"{REQUESTS}{request_id}/withdraw/").status_code, 404)
        withdrawn = write(self.alice_client, "post", f"{REQUESTS}{request_id}/withdraw/")
        self.assertEqual(withdrawn.status_code, 200, withdrawn.content)
        self.assertEqual(withdrawn.json()["status"], "withdrawn")
        self.assertEqual(decide(self.admin_client, request_id).status_code, 409)

    def test_approval_creates_once_for_applicant_and_preserves_tenant_isolation(self):
        self.use_mode("request_approval")
        request_id = self.make_request()
        self.assertEqual(decide(session(self.superuser), request_id).status_code, 403)
        approved = decide(self.admin_client, request_id)
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(approved.json()["status"], "approved")
        organization = self.assert_workspace(approved.json()["organization_id"], self.alice)
        self.assertEqual(self.alice_client.get(f"{ORGANIZATIONS}{organization.pk}/").status_code, 200)
        self.assertEqual(self.admin_client.get(f"{ORGANIZATIONS}{organization.pk}/").status_code, 404)
        self.assertEqual(decide(self.admin_client, request_id).status_code, 409)
        self.assertEqual(Organization.objects.filter(name=organization.name).count(), 1)
        notices = self.alice_client.get(NOTIFICATIONS).json()["items"]
        self.assertEqual([item["kind"] for item in notices], ["platform.creation_approved"])

    def test_rejection_self_review_and_inactive_applicant(self):
        self.use_mode("request_approval")
        own_request = self.make_request(self.admin_client, "Admin self request")
        self.assertEqual(decide(self.admin_client, own_request).status_code, 403)
        request_id = self.make_request()
        rejected = decide(self.admin_client, request_id, "reject", "Please revise the purpose")
        self.assertEqual(rejected.status_code, 200, rejected.content)
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual(rejected.json()["decision_comment"], "Please revise the purpose")
        self.assertIsNone(rejected.json()["organization_id"])
        self.assertFalse(Organization.objects.filter(name="Proposed workspace").exists())
        next_request = self.make_request(name="Inactive applicant workspace")
        self.alice.is_active = False
        self.alice.save(update_fields=["is_active"])
        self.assertEqual(decide(self.admin_client, next_request).status_code, 409)
        self.assertFalse(Organization.objects.filter(name="Inactive applicant workspace").exists())

    def test_platform_audit_failure_rolls_back_approved_organization_and_request(self):
        self.use_mode("request_approval")
        request_id = self.make_request()
        with patch("apps.organizations.platform.record_platform", side_effect=RuntimeError("audit failure")):
            with self.assertRaisesRegex(RuntimeError, "audit failure"):
                decide(self.admin_client, request_id)
        request = self.admin_client.get(f"{REVIEW}{request_id}/").json()
        self.assertEqual(request["status"], "pending")
        self.assertIsNone(request["organization_id"])
        self.assertFalse(Organization.objects.filter(name="Proposed workspace").exists())

    def test_duplicate_pending_requests_do_not_create_another_record(self):
        self.use_mode("request_approval")
        request_id = self.make_request()
        duplicate = submit(self.alice_client)
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertEqual([row["id"] for row in self.alice_client.get(REQUESTS).json()["items"]], [request_id])

    def test_daily_limit_cannot_be_bypassed_by_withdrawing_each_request(self):
        self.use_mode("request_approval")
        for index in range(5):
            request_id = self.make_request(name=f"Withdrawn workspace {index}")
            response = write(self.alice_client, "post", f"{REQUESTS}{request_id}/withdraw/")
            self.assertEqual(response.status_code, 200, response.content)
        limited = submit(self.alice_client)
        self.assertEqual(limited.status_code, 429, limited.content)
        self.assertEqual(self.alice_client.get(REQUESTS).json()["total"], 5)

    def test_platform_audit_and_notices_stay_out_of_tenant_and_foreign_inboxes(self):
        self.use_mode("request_approval")
        request_id = self.make_request()
        submitted = self.admin_client.get(NOTIFICATIONS).json()["items"]
        self.assertEqual([item["kind"] for item in submitted], ["platform.creation_submitted"])
        self.assertEqual(self.alice_client.get(NOTIFICATIONS).json()["items"], [])
        self.assertEqual(decide(self.admin_client, request_id, "reject", "Private reason").status_code, 200)
        notices = self.alice_client.get(NOTIFICATIONS).json()["items"]
        self.assertEqual([item["kind"] for item in notices], ["platform.creation_rejected"])
        notice = notices[0]
        self.assertLess(notice["id"], 0)
        self.assertIsNone(notice["organization_id"])
        self.assertNotIn("Private reason", str(notice))
        url = f'{NOTIFICATIONS}{notice["id"]}/'
        self.assertEqual(self.alice_client.get(f"{url}link/").status_code, 200)
        self.assertEqual(self.admin_client.get(f"{url}link/").status_code, 404)
        bob = session(self.bob)
        self.assertEqual(bob.get(NOTIFICATIONS).json()["items"], [])
        self.assertEqual(write(bob, "post", f"{url}read/").status_code, 404)
        self.assertEqual(self.alice_client.get(NOTIFICATIONS, {"org_id": self.existing.pk}).json()["items"], [])
        self.assertEqual(self.alice_client.get(f"{NOTIFICATIONS}count/").json()["unread"], 1)
        self.assertEqual(write(self.alice_client, "post", f"{url}read/").status_code, 200)
        self.assertEqual(self.alice_client.get(f"{NOTIFICATIONS}count/").json()["unread"], 0)
        self.assertTrue(PlatformAuditEvent.objects.filter(
            actor=self.admin, action="platform.creation_rejected", subject_id=request_id,
        ).exists())
        submitted_url = f'{NOTIFICATIONS}{submitted[0]["id"]}/link/'
        self.assertEqual(self.admin_client.get(submitted_url).status_code, 200)
        self.admin.groups.clear()
        self.assertEqual(self.admin_client.get(submitted_url).status_code, 404)
        self.assertEqual(self.admin_client.get(NOTIFICATIONS).json()["items"], [])


    def test_platform_and_organization_notices_share_pagination_and_read_all(self):
        self.use_mode("request_approval")
        request_id = self.make_request()
        approval = decide(self.admin_client, request_id)
        self.assertEqual(approval.status_code, 200, approval.content)
        organization = Organization.objects.get(pk=approval.json()["organization_id"])
        owner = organization.memberships.get(user=self.alice)
        task = Task.objects.create(organization=organization, created_by=owner, assignee=owner, name="Shared inbox")
        publish(
            organization_id=organization.pk, actor_id=self.admin.pk, kind="task.assigned",
            object_type="task", object_id=task.pk, recipient_ids=[self.alice.pk], key="platform-mixed-inbox",
        )
        pages = [
            self.alice_client.get(NOTIFICATIONS, {"page": page, "page_size": 1}).json()
            for page in (1, 2)
        ]
        self.assertEqual([page["total"] for page in pages], [2, 2])
        self.assertEqual([page["items"][0]["kind"] for page in pages], [
            "task.assigned", "platform.creation_approved",
        ])
        filtered = self.alice_client.get(NOTIFICATIONS, {"org_id": organization.pk}).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["kind"], "task.assigned")
        read = write(self.alice_client, "post", f"{NOTIFICATIONS}read-all/")
        self.assertEqual(read.status_code, 200, read.content)
        self.assertEqual(read.json(), {"updated": 2, "unread": 0})


class PlatformCreationConcurrencyTests(TransactionTestCase):
    def race(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            try:
                barrier.wait(timeout=10)
                response = operation()
                return response.status_code, response.content
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            return list(executor.map(run, operations))

    def test_concurrent_double_approval_creates_one_organization(self):
        self.check_decision_race("approve")

    def test_concurrent_approval_and_rejection_have_one_winner(self):
        self.check_decision_race("reject")

    def check_decision_race(self, competing_decision):
        admin = session(platform_admin("creation-race-admin"))
        second_admin = session(platform_admin("creation-race-reviewer"))
        applicant = account("creation-race-applicant")
        self.assertEqual(set_mode(admin, "request_approval").status_code, 200)
        submitted = submit(session(applicant))
        self.assertEqual(submitted.status_code, 201, submitted.content)
        request_id = submitted.json()["id"]

        results = self.race([
            lambda: decide(admin, request_id),
            lambda: decide(second_admin, request_id, competing_decision),
        ])

        self.assertEqual(sorted(status for status, _ in results), [200, 409], results)
        row = admin.get(f"{REVIEW}{request_id}/").json()
        if competing_decision == "approve":
            self.assertEqual(row["status"], "approved")
        self.assertIn(row["status"], ("approved", "rejected"))
        self.assertEqual(Organization.objects.filter(owner=applicant).count(), int(row["status"] == "approved"))

    def test_concurrent_direct_replays_create_one_organization(self):
        admin = session(platform_admin("creation-direct-race-admin"))
        applicant = account("creation-direct-race-applicant")
        self.assertEqual(set_mode(admin, "open").status_code, 200)
        first = session(applicant)
        second = session(applicant)
        payload = {"name": "Concurrent direct workspace", "idempotency_key": str(uuid4())}
        results = self.race([
            lambda: write(first, "post", ORGANIZATIONS, payload),
            lambda: write(second, "post", ORGANIZATIONS, payload),
        ])
        self.assertTrue(all(status in (200, 201) for status, _ in results), results)
        self.assertEqual(Organization.objects.filter(owner=applicant).count(), 1)
