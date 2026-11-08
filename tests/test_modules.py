from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department, DepartmentMembership
from apps.notifications.models import Notification, OrganizationNotificationPolicy
from apps.notifications.reminders import run_reminders
from apps.notifications.services import publish, retry_due
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.modules import ModuleDisabled
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership, StaffingRequest
from apps.projects.services import update_project
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"
OPTIONAL = {"projects", "staffing", "tasks", "calendar"}


def modules_url(organization):
    return f"{API}{organization.pk}/modules/"


class ModuleSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="modules-owner")
        cls.foreign_owner = users.create_superuser(username="modules-foreign-owner")
        cls.delegate = users.create_user(username="modules-delegate")
        cls.member = users.create_user(username="modules-ordinary")
        cls.organization = create_organization(user=cls.owner, name="Module settings")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign module settings")
        cls.delegate_member = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.delegate, can_manage_members=True,
        )
        cls.member_link = OrganizationMembership.objects.create(organization=cls.organization, user=cls.member)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = modules_url(self.organization)

    def change(self, changes, *, csrf=True):
        return write(self.client, "patch", self.url, {"changes": changes}, csrf=csrf)

    def catalog(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def enabled(self):
        return {row["code"] for row in self.catalog()["items"] if row["enabled"]}

    def audit(self):
        return AuditEvent.objects.filter(organization=self.organization, action="organization.module_changed")

    def test_catalog_defaults_include_all_optional_modules_and_protect_mandatory_core(self):
        self.client.force_login(self.owner)
        data = self.catalog()
        self.assertTrue(data["can_manage"])
        modules = {row["code"]: row for row in data["items"]}
        self.assertEqual({code for code, row in modules.items() if row["configurable"]}, OPTIONAL)
        self.assertEqual(self.organization.disabled_modules, [])
        for row in modules.values():
            with self.subTest(module=row["code"]):
                self.assertTrue(row["enabled"])
                self.assertTrue(row["name"].strip())
                self.assertTrue(row["description"].strip())
                self.assertTrue(set(row["dependencies"]) <= modules.keys())
        mandatory = [code for code, row in modules.items() if row["required"]]
        self.assertTrue(mandatory)
        self.assertTrue({"core", "audit", "notifications"} <= set(mandatory))
        self.assertIn("projects", modules["staffing"]["dependencies"])
        rejected = self.change({mandatory[0]: False, "calendar": False})
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertEqual(rejected.json()["code"], "required_module")
        self.assertIn("calendar", self.enabled())
        self.assertFalse(self.audit().exists())

        created = write(self.client, "post", API, {"name": "New module defaults"})
        self.assertEqual(created.status_code, 201, created.content)
        new = Organization.objects.get(pk=created.json()["id"])
        self.assertEqual(new.disabled_modules, [])
        self.assertTrue(all(row["enabled"] for row in self.client.get(modules_url(new)).json()["items"]))

    def test_explicit_dependency_change_is_atomic_and_audited_once_per_module(self):
        self.client.force_login(self.owner)
        rejected = self.change({"projects": False, "calendar": False})
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertEqual(rejected.json()["code"], "module_dependency")
        self.assertEqual(rejected.json()["dependency"], "projects")
        self.assertTrue(OPTIONAL <= self.enabled())
        self.assertFalse(self.audit().exists())

        changed = self.change({"projects": False, "staffing": False})
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(self.enabled() & OPTIONAL, {"tasks", "calendar"})
        self.organization.refresh_from_db()
        self.foreign.refresh_from_db()
        self.assertCountEqual(self.organization.disabled_modules, ["projects", "staffing"])
        self.assertEqual(self.foreign.disabled_modules, [])
        self.assertEqual(self.audit().count(), 2)
        self.assertEqual(self.audit().values("operation_id").distinct().count(), 1)
        self.assertEqual(set(self.audit().values_list("actor_id", flat=True)), {self.owner.pk})
        self.assertEqual({row.metadata["code"] for row in self.audit()}, {"projects", "staffing"})
        for row in self.audit():
            self.assertEqual(row.changes["is_enabled"], {"old": True, "new": False})
        replay = self.change({"projects": False, "staffing": False})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(self.audit().count(), 2)
        self.assertEqual(self.change({"staffing": True}).status_code, 409)
        self.assertEqual(self.enabled() & OPTIONAL, {"tasks", "calendar"})
        self.assertEqual(self.change({"projects": True, "staffing": True}).status_code, 200)
        self.assertTrue(OPTIONAL <= self.enabled())
        self.assertEqual(self.audit().count(), 4)

    def test_module_settings_tenant_role_and_csrf_guards(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.change({"tasks": False}).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.change({"tasks": False}).status_code, 404)
        for actor in (self.member, self.delegate):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                self.assertFalse(self.catalog()["can_manage"])
                self.assertEqual(self.change({"tasks": False}).status_code, 403)
        grant(self.organization, self.delegate_member, "organizations.modules.manage")
        self.client.force_login(self.delegate)
        self.assertTrue(self.catalog()["can_manage"])
        self.assertEqual(self.change({"tasks": False}, csrf=False).status_code, 403)
        unknown = self.change({"unknown": False})
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["code"], "unknown_module")
        self.assertEqual(self.change({"tasks": "false"}).status_code, 422)
        accepted = self.change({"tasks": False})
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(self.audit().get().actor_id, self.delegate.pk)
        self.client.force_login(self.member)
        self.assertFalse(self.catalog()["can_manage"])
        self.assertEqual(self.change({"tasks": True}).status_code, 403)

    def test_postgresql_rejects_unknown_duplicate_null_and_incoherent_module_arrays(self):
        for invalid in (["unknown"], ["tasks", "tasks"], [None], ["projects"]):
            with self.subTest(disabled=invalid), self.assertRaises(IntegrityError), transaction.atomic():
                Organization.objects.filter(pk=self.organization.pk).update(disabled_modules=invalid)
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.disabled_modules, [])

    def test_audit_write_failure_rolls_back_the_entire_module_change(self):
        self.client.force_login(self.owner)
        with patch("apps.organizations.modules.record", side_effect=IntegrityError("audit failed")):
            with self.assertRaisesRegex(IntegrityError, "audit failed"):
                self.change({"tasks": False, "calendar": False})
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.disabled_modules, [])
        self.assertFalse(self.audit().exists())


class ConcurrentModuleSettingsTests(TransactionTestCase):
    def test_concurrent_disjoint_changes_merge_without_lost_update(self):
        owner = get_user_model().objects.create_superuser(username="modules-concurrent-owner")
        organization = create_organization(user=owner, name="Concurrent modules")
        start = Barrier(2, timeout=8)

        def change(code):
            connection = connections["default"]
            try:
                client = Client(enforce_csrf_checks=True)
                client.force_login(owner)
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                start.wait()
                return write(client, "patch", modules_url(organization), {"changes": {code: False}}).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, code) for code in ("tasks", "calendar")]
            statuses = [future.result(timeout=16) for future in futures]
        self.assertEqual(statuses, [200, 200])
        organization.refresh_from_db()
        self.assertCountEqual(organization.disabled_modules, ["tasks", "calendar"])
        rows = AuditEvent.objects.filter(organization=organization, action="organization.module_changed")
        self.assertEqual({event.metadata["code"] for event in rows}, {"tasks", "calendar"})
        self.assertEqual(rows.values("operation_id").distinct().count(), 2)


class ModuleAvailabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="module-availability-owner")
        cls.other_owner = users.create_superuser(username="module-availability-foreign")
        cls.worker = users.create_user(username="module-availability-worker")
        cls.reader = users.create_user(username="module-availability-reader")
        cls.org = create_organization(user=cls.owner, name="Availability workspace")
        cls.foreign = create_organization(user=cls.other_owner, name="Unaffected workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.worker_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.worker)
        cls.reader_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.reader)
        cls.department = Department.objects.create(organization=cls.org, name="Availability department")
        cls.department_member = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.department, organization_membership=cls.worker_member,
        )
        cls.project = Project.objects.create(
            organization=cls.org, created_by=cls.owner, name="Sensitive module project",
            responsible_department=cls.department, status=Project.Status.ACTIVE,
        )
        cls.team = ProjectMembership.objects.create(
            organization=cls.org, project=cls.project, organization_membership=cls.worker_member,
        )
        cls.linked_task = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, assignee=cls.worker_member,
            name="Linked task", project=cls.project, department=cls.department,
            due_at=datetime.fromisoformat("2027-11-09T13:00:00+00:00"),
        )
        cls.standalone = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, assignee=cls.worker_member,
            name="Standalone task", due_at=cls.linked_task.due_at,
        )
        cls.staffing = StaffingRequest.objects.create(
            organization=cls.org, created_by=cls.owner, project=cls.project, department=cls.department,
            requested_count=1, description="Preserved request", status=StaffingRequest.Status.SENT,
            submitted_at=cls.linked_task.due_at,
        )
        for name, project in (("Linked event", cls.project), ("Standalone event", None)):
            CalendarEvent.objects.create(
                organization=cls.org, created_by=cls.owner_member, title=name,
                event_type=CalendarEvent.Type.MEETING, timezone="UTC", project=project,
                starts_at=datetime.fromisoformat("2027-11-09T13:00:00+00:00"),
                ends_at=datetime.fromisoformat("2027-11-09T14:00:00+00:00"),
            )
        cls.linked_event = cls.org.calendar_events.get(project=cls.project)
        cls.standalone_event = cls.org.calendar_events.get(project__isnull=True)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.base = f"{API}{self.org.pk}/"

    def change(self, changes):
        response = write(self.client, "patch", modules_url(self.org), {"changes": changes})
        self.assertEqual(response.status_code, 200, response.content)

    def assert_disabled(self, response, module):
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "module_disabled")
        self.assertEqual(response.json()["module"], module)

    def calendar(self):
        response = self.client.get(f"{self.base}calendar/", {
            "start": "2027-11-09", "end": "2027-11-10", "timezone": "UTC",
        })
        self.assertEqual(response.status_code, 200, response.content)
        return {(row["source"], row["id"]) for row in response.json()["items"]}

    def test_at_11_project_and_staffing_are_blocked_in_api_html_and_service_then_restored(self):
        project = f"{self.base}projects/{self.project.pk}/"
        request = f"{self.base}staffing-requests/{self.staffing.pk}/"
        html = reverse("organization-projects", args=[self.org.pk])
        overview = reverse("organization-detail", args=[self.org.pk])
        self.assertEqual(self.client.get(project).status_code, 200)
        self.assertEqual(self.client.get(request).status_code, 200)
        self.assertContains(self.client.get(overview), f'href="{html}"')
        self.assertEqual(write(self.client, "patch", project, {"description": "Preserved history"}).status_code, 200)
        history = f"{self.base}audit/objects/project/{self.project.pk}/"
        event = AuditEvent.objects.get(organization=self.org, object_type="project", object_id=self.project.pk)
        self.assertEqual(self.client.get(history).status_code, 200)
        self.change({"projects": False, "staffing": False})
        self.assert_disabled(self.client.get(project), "projects")
        self.assert_disabled(write(self.client, "patch", project, {"name": "Unauthorized rename"}), "projects")
        self.assert_disabled(self.client.get(request), "staffing")
        self.assert_disabled(self.client.get(f"{project}staffing-requests/"), "staffing")
        self.assertEqual(self.client.get(html).status_code, 409)
        dashboard = self.client.get(overview)
        self.assertNotContains(dashboard, f'href="{html}"')
        self.assertNotContains(dashboard, self.project.name)
        self.assertEqual(self.client.get(history).status_code, 404)
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())
        with self.assertRaises(ModuleDisabled) as disabled:
            update_project(user=self.owner, org_id=self.org.pk, project_id=self.project.pk, changes={"name": "Denied"})
        self.assertEqual(disabled.exception.module_code, "projects")
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(project).status_code, 404)
        self.assertEqual(self.client.get(f"{API}{self.foreign.pk}/projects/").status_code, 200)

        self.project.refresh_from_db()
        self.team.refresh_from_db()
        self.staffing.refresh_from_db()
        self.assertEqual(self.project.name, "Sensitive module project")
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertTrue(self.team.is_active)
        self.assertEqual(self.staffing.status, StaffingRequest.Status.SENT)
        self.client.force_login(self.owner)
        self.change({"projects": True, "staffing": True})
        self.assertEqual(self.client.get(project).json()["id"], self.project.pk)
        self.assertEqual(self.client.get(request).json()["id"], self.staffing.pk)
        self.assertEqual([row["id"] for row in self.client.get(history).json()["items"]], [event.pk])
        self.assertContains(self.client.get(overview), f'href="{html}"')

    def test_projects_off_hide_linked_tasks_and_events_but_leave_independent_modules_working(self):
        self.change({"projects": False, "staffing": False})
        tasks = self.client.get(f"{self.base}tasks/")
        self.assertEqual(tasks.status_code, 200, tasks.content)
        self.assertEqual([row["id"] for row in tasks.json()["items"]], [self.standalone.pk])
        self.assertEqual(self.client.get(f"{self.base}tasks/{self.linked_task.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}events/{self.linked_event.pk}/").status_code, 404)
        self.assert_disabled(self.client.get(f"{self.base}tasks/", {"project_id": self.project.pk}), "projects")
        self.assert_disabled(self.client.get(f"{self.base}calendar/", {
            "project_id": self.project.pk, "start": "2027-11-09", "end": "2027-11-10",
        }), "projects")
        self.assert_disabled(write(self.client, "post", f"{self.base}tasks/", {
            "name": "Blocked linked task", "project_id": self.project.pk,
        }), "projects")
        self.assert_disabled(write(self.client, "post", f"{self.base}events/", {
            "title": "Blocked linked event", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-09T16:00:00+00:00", "ends_at": "2027-11-09T17:00:00+00:00",
            "project_id": self.project.pk,
        }), "projects")
        self.assertEqual(self.calendar(), {
            ("task_deadline", self.standalone.pk), ("meeting", self.standalone_event.pk),
        })
        self.assertEqual(write(self.client, "patch", f"{self.base}tasks/{self.standalone.pk}/", {
            "name": "Renamed standalone task",
        }).status_code, 200)
        self.assertEqual(write(self.client, "patch", f"{self.base}events/{self.standalone_event.pk}/", {
            "title": "Renamed standalone event",
        }).status_code, 200)
        self.change({"projects": True, "staffing": True})
        self.assertEqual(self.calendar(), {
            ("task_deadline", self.standalone.pk), ("task_deadline", self.linked_task.pk),
            ("meeting", self.standalone_event.pk), ("meeting", self.linked_event.pk),
        })

    def test_reenable_does_not_restore_a_role_revoked_while_the_module_was_off(self):
        role = grant(self.org, self.reader_member, "projects.view")
        project = f"{self.base}projects/{self.project.pk}/"
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(project).status_code, 200)
        self.client.force_login(self.owner)
        self.change({"projects": False, "staffing": False})
        revoked = write(self.client, "delete", f"{self.base}role-assignments/{role.pk}/")
        self.assertEqual(revoked.status_code, 204, revoked.content)
        self.change({"projects": True, "staffing": True})
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(project).status_code, 404)
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(project).status_code, 200)

    def test_disabled_project_notifications_are_hidden_and_pending_delivery_waits_for_reenable(self):
        now = datetime.fromisoformat("2027-11-09T12:00:00+00:00")
        payload = {
            "organization_id": self.org.pk, "actor_id": self.owner.pk, "kind": "project.allocated",
            "object_type": "project", "object_id": self.project.pk, "recipient_ids": [self.worker.pk],
        }
        visible = publish(**payload, key="modules:existing-project", now=now).notifications.get()
        delayed = publish(
            **payload, key="modules:pending-project", now=now, deliver_after=now + timedelta(minutes=1),
        )
        self.change({"projects": False, "staffing": False})
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get("/api/v1/notifications/").json()["items"], [])
        self.assertEqual(self.client.get("/api/v1/notifications/count/").json()["unread"], 0)
        self.assertEqual(self.client.get(f"/api/v1/notifications/{visible.pk}/link/").status_code, 404)
        retry_due(now=now + timedelta(minutes=1))
        delayed.refresh_from_db()
        self.assertIsNone(delayed.delivered_at)
        self.assertEqual(delayed.delivery_attempts, 0)
        self.assertFalse(delayed.notifications.exists())

        self.client.force_login(self.owner)
        self.change({"projects": True, "staffing": True})
        retry_due(now=now + timedelta(days=1))
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get("/api/v1/notifications/count/").json()["unread"], 2)
        self.assertEqual(self.client.get(f"/api/v1/notifications/{visible.pk}/link/").status_code, 200)

    def test_reminder_worker_skips_disabled_and_project_linked_objects(self):
        now = datetime.fromisoformat("2027-11-09T12:00:00+00:00")
        OrganizationNotificationPolicy.objects.create(
            organization=self.org, task_offsets_minutes=[60], calendar_offsets_minutes=[60],
        )
        for event in (self.linked_event, self.standalone_event):
            event.invitations.create(
                organization=self.org, recipient=self.worker_member, invited_by=self.owner_member,
            )
        self.change({"projects": False, "staffing": False})
        run_reminders(now=now)
        notices = Notification.objects.filter(
            organization=self.org, event__kind__in=["task.reminder", "calendar.reminder"],
        )
        self.assertCountEqual(notices.values_list("event__kind", "event__object_id"), [
            ("task.reminder", self.standalone.pk), ("calendar.reminder", self.standalone_event.pk),
        ])
        self.change({"tasks": False, "calendar": False})
        self.assert_disabled(self.client.get(f"{self.base}tasks/"), "tasks")
        self.assert_disabled(self.client.get(f"{self.base}events/{self.standalone_event.pk}/"), "calendar")
        self.assertEqual(self.client.get(reverse("organization-tasks", args=[self.org.pk])).status_code, 409)
        self.assertEqual(self.client.get(reverse("organization-calendar", args=[self.org.pk])).status_code, 409)
        self.assertEqual(self.client.get(f"{self.base}departments/").status_code, 200)
        self.assertEqual(self.client.get(f"{self.base}audit/").status_code, 200)
        self.assertEqual(self.client.get(reverse("organization-detail", args=[self.org.pk])).status_code, 200)

        Task.objects.filter(pk=self.standalone.pk).update(due_at=now + timedelta(hours=2))
        CalendarEvent.objects.filter(pk=self.standalone_event.pk).update(
            starts_at=now + timedelta(hours=2), ends_at=now + timedelta(hours=3),
        )
        run_reminders(now=now + timedelta(hours=1))
        self.assertEqual(notices.count(), 2)
        self.change({"tasks": True, "calendar": True})
        self.assertEqual(self.client.get(f"{self.base}tasks/{self.standalone.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"{self.base}events/{self.standalone_event.pk}/").status_code, 200)
