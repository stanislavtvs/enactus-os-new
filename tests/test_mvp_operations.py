from datetime import datetime, timedelta
from io import StringIO
import signal
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from apps.access.policy import Access
from apps.audit.models import AuditEvent
from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department, DepartmentMembership
from apps.notifications.models import Notification, OrganizationNotificationPolicy
from apps.notifications.reminders import run_reminders
from apps.notifications.services import publish, retry_due
from apps.organizations.services import create_organization
from apps.projects.models import Project
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


NOW = datetime.fromisoformat("2027-11-15T16:00:00+00:00")


class MVPOperationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="mvp-operations-owner")
        cls.worker = users.create_user(username="mvp-operations-worker")
        cls.org = create_organization(user=cls.owner, name="MVP operations")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.member = cls.org.memberships.create(user=cls.worker)
        cls.department = Department.objects.create(organization=cls.org, name="Operations")
        DepartmentMembership.objects.create(
            organization=cls.org, department=cls.department, organization_membership=cls.member,
        )
        cls.project = Project.objects.create(organization=cls.org, name="Calendar project", created_by=cls.owner)
        cls.project.memberships.create(organization=cls.org, organization_membership=cls.member)

    def test_calendar_audiences_move_rsvp_and_scheduled_reminder(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        base = f"/api/v1/organizations/{self.org.pk}/events/"
        OrganizationNotificationPolicy.objects.create(organization=self.org, calendar_offsets_minutes=[60])
        response = write(client, "post", base, {
            "title": "MVP calendar series", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T18:00:00+00:00", "ends_at": "2027-11-15T19:00:00+00:00",
            "rsvp_mode": "required", "recurrence": {"frequency": "daily", "count": 2},
        })
        self.assertEqual(response.status_code, 201, response.content)
        event = CalendarEvent.objects.get(pk=response.json()["id"])
        response = write(client, "post", f"{base}{event.pk}/invitations/", {
            "departments": [{"id": self.department.pk, "include_descendants": False}],
            "project_ids": [self.project.pk],
        })
        self.assertEqual(response.status_code, 201, response.content)
        invitation = event.invitations.get()
        self.assertEqual(invitation.recipient_id, self.member.pk)
        self.assertEqual(invitation.sources.count(), 2)

        inbox = f"/api/v1/organizations/calendar/invitations/{invitation.pk}/"
        client.force_login(self.worker)
        response = write(client, "post", f"{inbox}respond/", {
            "response": "yes", "expected_revision": event.rsvp_revision, "expected_response_at": None,
        })
        self.assertEqual(response.status_code, 200, response.content)
        client.force_login(self.owner)
        response = write(client, "patch", f"{base}{event.pk}/occurrences/2027-11-15/", {
            "expected_version": 0, "starts_at": "2027-11-15T17:00:00+00:00",
            "ends_at": "2027-11-15T18:00:00+00:00",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Notification.objects.filter(
            recipient=self.worker, event__kind="calendar.occurrence_rescheduled", event__object_id=event.pk,
        ).count(), 1)

        client.force_login(self.worker)
        occurrence = f"{inbox}occurrences/2027-11-15/"
        response = write(client, "post", f"{occurrence}respond/", {
            "response": "maybe", "expected_revision": event.rsvp_revision,
            "expected_occurrence_revision": 1, "expected_response_at": None,
        })
        self.assertEqual(response.status_code, 200, response.content)
        # Reminder policy delivers only to attendees who confirmed.
        response = write(client, "post", f"{occurrence}respond/", {
            "response": "yes", "expected_revision": event.rsvp_revision,
            "expected_occurrence_revision": 1, "expected_response_at": response.json()["responded_at"],
        })
        self.assertEqual(response.status_code, 200, response.content)
        sister = client.get(f"{inbox}occurrences/2027-11-16/").json()
        self.assertEqual(sister["response"], "yes")
        self.assertEqual(sister["occurrence_rsvp_revision"], 0)

        for moment in (NOW - timedelta(seconds=1), NOW, NOW):
            run_reminders(now=moment)
        reminder = Notification.objects.get(
            recipient=self.worker, event__kind="calendar.reminder", event__object_id=event.pk,
        )
        self.assertEqual(reminder.event.data["occurrence_key"], "2027-11-15")
        self.assertEqual(AuditEvent.objects.filter(
            organization=self.org, object_type="calendar_event", object_id=event.pk,
            action="calendar_event.occurrence_updated",
        ).count(), 1)

    def test_outage_windows_keep_business_notifications_and_skip_stale_reminders(self):
        OrganizationNotificationPolicy.objects.create(organization=self.org, task_offsets_minutes=[180])
        for minutes in (5, 30, 120, 720):
            with self.subTest(minutes=minutes):
                now = NOW + timedelta(days=minutes)
                task = Task.objects.create(
                    organization=self.org, created_by=self.owner_member, assignee=self.member,
                    name=f"Outage for {minutes} minutes", due_at=now + timedelta(minutes=180 - minutes),
                )
                event = publish(
                    organization_id=self.org.pk, actor_id=self.owner.pk,
                    object_type="task", object_id=task.pk, kind="task.assigned",
                    recipient_ids=[self.worker.pk], key=f"mvp-outage:{minutes}",
                    now=now - timedelta(minutes=minutes, seconds=1), deliver_after=now,
                )
                self.assertFalse(event.notifications.exists())
                for _ in range(2):
                    retry_due(now=now)
                    run_reminders(now=now)
                self.assertEqual(event.notifications.count(), 1)
                self.assertEqual(Notification.objects.filter(
                    organization=self.org, event__object_id=task.pk, event__kind="task.reminder",
                ).count(), int(minutes <= 60))

    def test_worker_retries_database_outage_without_logging_credentials(self):
        module = "apps.notifications.management.commands.notification_worker"
        handlers = {}
        attempts = []
        stderr = StringIO()

        def install(sig, handler):
            previous = handlers.get(sig, signal.SIG_DFL)
            handlers[sig] = handler
            return previous

        def retry(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise OperationalError("sensitive connection information")
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with (
            patch(f"{module}.signal.signal", side_effect=install),
            patch(f"{module}.retry_due", side_effect=retry),
            patch(f"{module}.close_old_connections"),
            patch(f"{module}.sleep"),
            patch(f"{module}.monotonic", side_effect=[0, 2, 3, 5]),
        ):
            call_command("notification_worker", interval=1, stderr=stderr)
        self.assertEqual(len(attempts), 2)
        self.assertIn("Retrying", stderr.getvalue())
        self.assertNotIn("sensitive connection", stderr.getvalue())

    def test_audit_search_at_volume_keeps_hidden_counts_private(self):
        grant(self.org, self.member, "audit.read_scoped", "tasks.view", department=self.department)
        other = Department.objects.create(organization=self.org, name="Private branch")
        tasks = Task.objects.bulk_create([
            Task(organization=self.org, created_by=self.owner_member, department=department, name=f"Bench {i}")
            for department in (self.department, other) for i in range(120)
        ])
        AuditEvent.objects.bulk_create([
            AuditEvent(
                organization=self.org, actor=self.owner, department_id=task.department_id,
                object_type="task", object_id=task.pk, action="task.updated",
            )
            for task in tasks for _ in range(10)
        ])
        client = Client()
        client.force_login(self.worker)
        route = f"/api/v1/organizations/{self.org.pk}/audit/"
        queries = []

        def count_query(execute, sql, params, many, context):
            queries.append(sql)
            return execute(sql, params, many, context)

        start = perf_counter()
        with connection.execute_wrapper(count_query):
            response = client.get(route, {"q": "Bench", "page": 2, "page_size": 20})
        elapsed = perf_counter() - start
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 1200)
        self.assertEqual(len(response.json()["items"]), 20)
        self.assertLessEqual(len(queries), 35)
        self.assertGreater(len(queries), 0)
        self.assertEqual(client.get(route, {"department_id": other.pk}).json()["total"], 0)
        self.assertEqual(client.get(route, {"date_to": "2020-01-01"}).json()["total"], 0)
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_auditevent'")
            indexes = {row[0] for row in cursor.fetchall()}
        self.assertTrue({"audit_org_timeline_idx", "audit_department_timeline_idx"} <= indexes)
        print(f"MVP audit benchmark: 2400 events, {len(queries)} SQL queries, {elapsed * 1000:.1f} ms")

    def test_bounded_queries_on_mixed_workspace(self):
        from apps.projects.models import StaffingRequest
        from apps.tasks.models import TaskDependency

        teams = [self.department]
        parents = teams
        for level in range(4):
            parents = Department.objects.bulk_create([
                Department(organization=self.org, parent=parent, name=f"Division {level}-{index}")
                for parent in parents for index in range(3)
            ])
            teams += parents
        projects = Project.objects.bulk_create([
            Project(organization=self.org, created_by=self.owner, name=f"Project {index}",
                    responsible_department=department, status=Project.Status.ACTIVE)
            for index, department in enumerate(teams[:80])
        ])
        tasks = Task.objects.bulk_create([
            Task(organization=self.org, created_by=self.owner_member, department=teams[index % len(teams)],
                 name=f"Board task {index}", due_at=NOW + timedelta(days=2))
            for index in range(240)
        ])
        chain = tasks[0]
        for depth in range(10):
            chain = Task.objects.create(
                organization=self.org, created_by=self.owner_member, department=chain.department,
                name=f"Child {depth}", parent=chain,
            )
        TaskDependency.objects.bulk_create([
            TaskDependency(organization=self.org, task=tasks[index], predecessor=tasks[index + 1], kind="strict")
            for index in range(45)
        ])
        StaffingRequest.objects.bulk_create([
            StaffingRequest(
                organization=self.org, department=self.department, project=project, created_by=self.owner,
                status=StaffingRequest.Status.SENT, requested_count=2, submitted_at=NOW,
            )
            for project in projects[:40]
        ])
        users = get_user_model().objects.bulk_create([
            get_user_model()(username=f"mvp-volume-{index}") for index in range(100)
        ])
        self.org.memberships.bulk_create([
            self.org.memberships.model(organization=self.org, user=user) for user in users
        ])
        client = Client()
        client.force_login(self.owner)
        base = f"/api/v1/organizations/{self.org.pk}/"
        series_ids = []
        for index in range(8):
            response = write(client, "post", f"{base}events/", {
                "title": f"Volume series {index}", "type": "meeting", "timezone": "UTC",
                "starts_at": "2027-11-16T18:00:00+00:00", "ends_at": "2027-11-16T19:00:00+00:00",
                "recurrence": {"frequency": "daily", "count": 8},
            })
            self.assertEqual(response.status_code, 201, response.content)
            series_ids.append(response.json()["id"])
        sent = write(client, "post", f"{base}events/{series_ids[0]}/invitations/", {"organization": True})
        self.assertEqual(sent.status_code, 201, sent.content)

        def measure(label, get_response, maximum=40):
            count = 0

            def count_query(execute, sql, params, many, context):
                nonlocal count
                count += 1
                return execute(sql, params, many, context)

            start = perf_counter()
            with connection.execute_wrapper(count_query):
                response = get_response()
            elapsed = perf_counter() - start
            self.assertEqual(response.status_code, 200, response.content)
            self.assertGreater(count, 0)
            self.assertLessEqual(count, maximum, label)
            print(f"MVP {label}: {count} SQL queries, {elapsed * 1000:.1f} ms")
            return response.json()

        self.assertEqual(len(measure("departments / 121", lambda: client.get(f"{base}departments/"))), 121)
        self.assertEqual(len(measure("projects / 80", lambda: client.get(f"{base}projects/"))), 81)
        self.assertEqual(len(measure("filtered tasks / 250", lambda: client.get(
            f"{base}tasks/", {"q": "Board task", "page_size": 20},
        ))["items"]), 20)
        measure("depth-10 task", lambda: client.get(f"{base}tasks/{chain.pk}/"))
        measure("45 strict dependency edges", lambda: client.get(f"{base}tasks/{tasks[0].pk}/dependencies/"))
        self.assertEqual(len(measure("staffing / 40", lambda: client.get(f"{base}staffing-requests/"))), 40)
        self.assertEqual(len(measure("calendar / 64 occurrences", lambda: client.get(
            f"{base}events/", {"start": "2027-11-16", "end": "2027-11-24", "timezone": "UTC"},
        ))["items"]), 64)
        audience = measure("audience / 102", lambda: write(
            client, "post", f"{base}events/{series_ids[1]}/invitations/preview/", {"organization": True},
        ))
        self.assertEqual(audience["total"], 102)
        client.force_login(self.worker)
        measure("notifications", lambda: client.get("/api/v1/notifications/"))


        grant(self.org, self.member, "tasks.view", department=self.department, descendants=True)
        access = Access(self.worker, self.org)
        started = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(all(access.allows("tasks.view", team) for team in teams))
        self.assertGreater(len(queries), 0)
        self.assertLessEqual(len(queries), 4)
        print(f"MVP inherited RBAC / {len(teams)} departments: {len(queries)} SQL queries, "
              f"{(perf_counter() - started) * 1000:.1f} ms")
