from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.departments.models import Department
from apps.notifications.models import NotificationEvent, OrganizationNotificationPolicy
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.onboarding import issue_invitation
from apps.organizations.services import create_organization
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


class NotificationPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="notification-policy-owner")
        cls.foreign_owner = users.create_superuser(username="notification-policy-foreign")
        cls.member = users.create_user(username="notification-policy-member")
        cls.delegate = users.create_user(username="notification-policy-delegate")
        cls.prejoin = users.create_user(username="notification-policy-prejoin")
        cls.org = create_organization(user=cls.owner, name="Notification policy")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Other notification policy")
        cls.member_link = OrganizationMembership.objects.create(organization=cls.org, user=cls.member)
        cls.foreign_member_link = OrganizationMembership.objects.create(organization=cls.foreign, user=cls.member)
        cls.delegate_link = OrganizationMembership.objects.create(organization=cls.org, user=cls.delegate)
        cls.department = Department.objects.create(organization=cls.org, name="Notification policy department")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.base = f"/api/v1/organizations/{self.org.pk}/"
        self.policy = f"{self.base}notification-policy/"
        self.preference = f"{self.base}notification-preferences/"
        self.other_preference = f"/api/v1/organizations/{self.foreign.pk}/notification-preferences/"

    def test_policy_management_requires_active_tenant_and_organization_scope(self):
        self.assertEqual(self.client.get(self.policy).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.policy).status_code, 404)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.policy).status_code, 403)
        self.assertEqual(write(self.client, "patch", self.policy, {"task_overdue": True}).status_code, 403)
        self.client.force_login(self.owner)
        defaults = self.client.get(self.policy)
        self.assertEqual(defaults.status_code, 200, defaults.content)
        self.assertEqual(defaults.headers["Cache-Control"], "no-store")
        self.assertEqual(defaults.json()["policy"]["task_offsets_minutes"], [])
        self.assertFalse(OrganizationNotificationPolicy.objects.filter(organization=self.org).exists())
        self.assertEqual(
            write(self.client, "patch", self.policy, {"task_overdue": True}, csrf=False).status_code,
            403,
        )

        grant(self.org, self.delegate_link, "notifications.manage", department=self.department)
        self.client.force_login(self.delegate)
        self.assertEqual(self.client.get(self.policy).status_code, 403)
        grant(self.org, self.delegate_link, "notifications.manage")
        changed = write(self.client, "patch", self.policy, {
            "task_offsets_minutes": [1440, 60], "task_overdue": True,
        })
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.json()["policy"]["task_offsets_minutes"], [1440, 60])
        self.assertTrue(changed.json()["policy"]["task_overdue"])
        self.assertFalse(OrganizationNotificationPolicy.objects.filter(organization=self.foreign).exists())

    def test_invalid_policy_intervals_and_unknown_kinds_are_atomic(self):
        self.client.force_login(self.owner)
        for fields in (
            {"task_offsets_minutes": [15]}, {"task_offsets_minutes": [60, 60]},
            {"calendar_offsets_minutes": [0]}, {"enabled_categories": ["calendar", "calendar"]},
            {"disabled_kinds": ["task.unknown"]}, {"all_day_reminder_time": "09:00:01"},
        ):
            with self.subTest(fields=fields):
                rejected = write(self.client, "patch", self.policy, {"task_overdue": True, **fields})
                self.assertEqual(rejected.status_code, 422, rejected.content)
        self.assertFalse(OrganizationNotificationPolicy.objects.filter(organization=self.org).exists())

    def test_preferences_are_per_active_membership_and_reject_impersonation(self):
        self.client.force_login(self.member)
        read = self.client.get(self.preference)
        self.assertEqual(read.status_code, 200, read.content)
        self.assertEqual(read.headers["Cache-Control"], "no-store")
        defaults = read.json()["preferences"]
        self.assertEqual(defaults["timezone"], "UTC")
        payload = {"timezone": "America/New_York", "quiet_start": "22:00", "quiet_end": "08:00"}
        self.assertEqual(write(self.client, "patch", self.preference, payload, csrf=False).status_code, 403)
        saved = write(self.client, "patch", self.preference, payload)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["preferences"]["timezone"], "America/New_York")
        self.assertEqual(self.client.get(self.other_preference).json()["preferences"], defaults)

        for invalid in (
            {"user_id": self.delegate.pk}, {"timezone": "GMT+5"},
            {"quiet_start": None}, {"quiet_start": "08:00"}, {"quiet_end": "08:00:01"},
        ):
            with self.subTest(invalid=invalid):
                response = write(self.client, "patch", self.preference, invalid)
                self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(self.client.get(self.preference).json()["preferences"], saved.json()["preferences"])

        self.client.force_login(self.delegate)
        self.assertEqual(self.client.get(self.preference).json()["preferences"], defaults)
        self.assertEqual(self.client.get(self.other_preference).status_code, 404)
        self.client.force_login(self.member)
        OrganizationMembership.objects.filter(pk=self.member_link.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.preference).status_code, 404)
        self.assertEqual(write(self.client, "patch", self.preference, payload).status_code, 404)
        self.assertEqual(self.client.get(self.other_preference).status_code, 200)

    def test_org_and_personal_mutes_do_not_prevent_assignment_or_prejoin_invitation(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        task = Task.objects.create(
            organization=self.org, created_by=self.org.memberships.get(user=self.owner), name="Muted assignment",
        )
        self.client.force_login(self.member)
        personal = write(self.client, "patch", self.preference, {"enabled_categories": ["calendar"]})
        self.assertEqual(personal.status_code, 200, personal.content)
        self.client.force_login(self.owner)
        assigned = write(
            self.client, "put", f"{self.base}tasks/{task.pk}/assignee/", {"assignee_id": self.member_link.pk},
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.member_link.pk)
        self.assertFalse(NotificationEvent.objects.filter(kind="task.assigned", notifications__recipient=self.member).exists())

        saved = write(self.client, "patch", self.policy, {"disabled_kinds": ["organization.invited"]})
        self.assertEqual(saved.status_code, 200, saved.content)
        invitation, _ = issue_invitation(actor=self.owner, org_id=self.org.pk, username=self.prejoin.username)
        self.assertEqual(invitation.recipient_id, self.prejoin.pk)
        self.assertFalse(NotificationEvent.objects.filter(
            kind="organization.invited", notifications__recipient=self.prejoin,
        ).exists())
