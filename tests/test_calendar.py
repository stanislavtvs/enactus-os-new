from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task
from tests.test_collection_pages import CollectionMarkup
from tests.test_department_memberships import write
from tests.test_projects import grant


class CalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="calendar-owner")
        cls.foreign_owner = users.create_superuser(username="calendar-foreign-owner")
        cls.reader = users.create_user(username="calendar-reader")
        cls.scoped = users.create_user(username="calendar-scoped")
        cls.no_access = users.create_user(username="calendar-no-access")
        cls.org = create_organization(user=cls.owner, name="Calendar workspace")
        cls.foreign_org = create_organization(user=cls.foreign_owner, name="Other calendar")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign_org.memberships.get(user=cls.foreign_owner)
        cls.reader_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.reader)
        cls.scoped_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.scoped)
        OrganizationMembership.objects.create(organization=cls.org, user=cls.no_access)
        cls.department = Department.objects.create(organization=cls.org, name="Calendar branch")
        cls.child_department = Department.objects.create(
            organization=cls.org, name="Calendar child", parent=cls.department,
        )
        cls.other_department = Department.objects.create(organization=cls.org, name="Other branch")
        cls.foreign_department = Department.objects.create(
            organization=cls.foreign_org, name="Foreign calendar department",
        )
        cls.project = Project.objects.create(
            organization=cls.org, name="Calendar project", created_by=cls.owner,
            responsible_department=cls.department,
        )
        cls.foreign_project = Project.objects.create(
            organization=cls.foreign_org, name="Foreign calendar project", created_by=cls.foreign_owner,
        )
        grant(cls.org, cls.reader_member, "calendar.read")
        grant(
            cls.org, cls.scoped_member, "calendar.read", "calendar.create", "calendar.manage_scope",
            department=cls.department, descendants=True,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.base = self.org_api(self.org)
        self.calendar_url = f"{self.base}calendar/"
        self.events_url = f"{self.base}events/"

    def org_api(self, organization):
        return f"/api/v1/organizations/{organization.pk}/"

    def event_url(self, event, action=""):
        return f"{self.events_url}{event['id']}/{action}"

    def create_event(self, title="Planning meeting", **fields):
        return write(self.client, "post", self.events_url, {
            "title": title, "type": "meeting",
            "starts_at": "2026-11-09T09:00:00+05:00", "ends_at": "2026-11-09T10:00:00+05:00",
            "timezone": "Asia/Almaty", **fields,
        })

    def make_event(self, title="Planning meeting", **fields):
        response = self.create_event(title, **fields)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def fetch_calendar(self, start="2026-11-09", end="2026-11-10", **filters):
        return self.client.get(self.calendar_url, {
            "start": start, "end": end, "timezone": "Asia/Almaty", **filters,
        })

    def items(self, *args, **filters):
        response = self.fetch_calendar(*args, **filters)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def test_event_lifecycle_keeps_identity_and_separates_action_permissions(self):
        self.client.force_login(self.owner)
        event = self.make_event(description="Team plan", location="Room A")
        self.assertEqual(event["type"], "meeting")
        other = self.make_event("Launch", type="event")
        self.assertEqual(other["type"], "event")
        self.assertEqual({(row["source"], row["id"]) for row in self.items()}, {
            ("meeting", event["id"]), ("event", other["id"]),
        })
        response = write(self.client, "patch", self.event_url(event), {
            "title": "Updated meeting", "location": "Room B", "online_url": "https://example.com/meeting",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["id"], event["id"])

        self.client.force_login(self.reader)
        detail = self.client.get(self.event_url(event))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["title"], "Updated meeting")
        self.assertEqual(detail.json()["location"], "Room B")
        row = self.items(source="meeting")[0]
        self.assertEqual(row["description"], "Team plan")
        for action in ("can_edit", "can_cancel", "can_archive"):
            self.assertFalse(row[action])
        self.assertEqual(
            write(self.client, "patch", self.event_url(event), {"title": "Denied"}).status_code, 403,
        )
        self.assertEqual(write(self.client, "post", self.event_url(event, "cancel/"), {}).status_code, 403)
        self.assertEqual(write(self.client, "post", self.event_url(event, "archive/"), {}).status_code, 403)

        self.client.force_login(self.owner)
        cancelled = write(self.client, "post", self.event_url(event, "cancel/"), {})
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(self.items(source="meeting")[0]["status"], "cancelled")
        self.assertEqual(self.client.get(self.event_url(event)).json()["status"], "cancelled")
        archived = write(self.client, "post", self.event_url(event, "archive/"), {})
        self.assertEqual(archived.status_code, 200, archived.content)
        self.assertEqual(self.items(source="meeting"), [])
        rows = self.items(source="meeting", include_archived=True)
        self.assertEqual([row["id"] for row in rows], [event["id"]])
        self.assertFalse(rows[0]["can_edit"])
        self.assertEqual(self.client.get(self.event_url(event)).json()["title"], "Updated meeting")

    def test_date_range_uses_exclusive_end_and_keeps_overlapping_multiday_events(self):
        self.client.force_login(self.owner)
        event = self.make_event(
            "Overnight", starts_at="2026-11-08T22:00:00+05:00", ends_at="2026-11-10T01:00:00+05:00",
        )
        self.make_event(
            "Ends exactly at start", starts_at="2026-11-08T21:00:00+05:00",
            ends_at="2026-11-09T00:00:00+05:00",
        )
        self.make_event(
            "Starts exactly at end", starts_at="2026-11-10T00:00:00+05:00",
            ends_at="2026-11-10T01:00:00+05:00",
        )
        self.assertEqual([row["id"] for row in self.items()], [event["id"]])
        self.assertEqual(len(self.items("2026-11-08", "2026-11-15")), 3)
        self.assertEqual(len(self.items("2026-11-01", "2026-12-01")), 3)
        self.assertEqual(self.items("2026-12-31", "2027-01-01"), [])

    def test_iana_day_window_handles_spring_forward_and_all_day_uses_civil_midnights(self):
        self.client.force_login(self.owner)
        all_day = self.make_event(
            "All-day DST", type="event", all_day=True, timezone="America/New_York",
            starts_at="2026-03-08T00:00:00-05:00", ends_at="2026-03-09T00:00:00-04:00",
        )
        late = self.make_event(
            "Last hour of DST day", timezone="UTC",
            starts_at="2026-03-09T03:30:00+00:00", ends_at="2026-03-09T03:45:00+00:00",
        )
        next_day = self.make_event(
            "Next day in New York", timezone="UTC",
            starts_at="2026-03-09T04:30:00+00:00", ends_at="2026-03-09T04:45:00+00:00",
        )
        rows = self.items("2026-03-08", "2026-03-09", timezone="America/New_York")
        self.assertEqual({row["id"] for row in rows}, {all_day["id"], late["id"]})
        row = next(row for row in rows if row["id"] == all_day["id"])
        self.assertTrue(row["all_day"])
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(
            datetime.fromisoformat(row["ends_at"]) - datetime.fromisoformat(row["starts_at"]),
            timedelta(hours=23),
        )
        utc_rows = self.items("2026-03-09", "2026-03-10", timezone="UTC", source="meeting")
        self.assertEqual({row["id"] for row in utc_rows}, {late["id"], next_day["id"]})
        self.assertEqual(
            self.items("2026-03-09", "2026-03-10", timezone="Pacific/Kiritimati", source="event"),
            [],
        )
        self.assertEqual(
            [row["id"] for row in self.items(
                "2026-03-08", "2026-03-09", timezone="Pacific/Kiritimati", source="event",
            )],
            [all_day["id"]],
        )
        autumn = self.make_event(
            "All-day fall back", type="event", all_day=True, timezone="America/New_York",
            starts_at="2026-11-01T00:00:00-04:00", ends_at="2026-11-02T00:00:00-05:00",
        )
        rows = self.items("2026-11-01", "2026-11-02", timezone="America/New_York", source="event")
        self.assertEqual([row["id"] for row in rows], [autumn["id"]])
        self.assertEqual(
            datetime.fromisoformat(rows[0]["ends_at"]) - datetime.fromisoformat(rows[0]["starts_at"]),
            timedelta(hours=25),
        )

        midnight_gap = self.make_event(
            "All-day with skipped midnight", type="event", all_day=True, timezone="America/Santiago",
            starts_at="2026-09-06T01:00:00-03:00", ends_at="2026-09-07T00:00:00-03:00",
        )
        rows = self.items("2026-09-06", "2026-09-07", timezone="America/Santiago", source="event")
        self.assertEqual([row["id"] for row in rows], [midnight_gap["id"]])
        self.assertEqual(
            self.items("2026-09-07", "2026-09-08", timezone="America/Santiago", source="event"), [],
        )

    def test_invalid_times_ranges_and_urls_are_rejected(self):
        self.client.force_login(self.owner)
        for invalid in (
            {"starts_at": "2026-11-09T09:00:00"},
            {"starts_at": "0001-01-01T00:00:00+14:00"},
            {"ends_at": "2026-11-09T08:00:00+05:00"},
            {"ends_at": "2026-11-09T09:00:00+05:00"},
            {"timezone": "GMT+5"},
            {"all_day": True},
            {"online_url": "javascript:alert(1)"},
            {"online_url": "https://example.com/" + "a" * 2048},
        ):
            with self.subTest(invalid=invalid):
                response = self.create_event(**invalid)
                self.assertEqual(response.status_code, 422, response.content)
        for start, end, zone in (
            ("2026-11-10", "2026-11-09", "UTC"),
            ("2026-11-09", "2026-11-09", "UTC"),
            ("2026-11-01", "2026-12-14", "UTC"),
            ("2026-11-01", "2026-11-02", "Imaginary/Zone"),
            ("0001-01-01", "0001-01-02", "UTC"),
            ("9999-12-30", "9999-12-31", "UTC"),
        ):
            with self.subTest(start=start, end=end, zone=zone):
                response = self.fetch_calendar(start, end, timezone=zone)
                self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(self.items(), [])

    def test_live_deadlines_include_subtasks_filter_scopes_and_never_create_calendar_records(self):
        self.client.force_login(self.owner)
        due = datetime.fromisoformat("2026-11-09T11:00:00+05:00")
        parent = Task.objects.create(
            organization=self.org, name="Deadline parent", created_by=self.owner_member, due_at=due,
            project=self.project, department=self.department,
        )
        child = Task.objects.create(
            organization=self.org, name="Deadline child", created_by=self.owner_member, due_at=due,
            parent=parent, project=self.project, department=self.department,
        )
        archived = Task.objects.create(
            organization=self.org, name="Archived deadline", created_by=self.owner_member,
            due_at=due, is_archived=True,
        )
        Task.objects.create(organization=self.org, name="No deadline", created_by=self.owner_member)
        Task.objects.create(
            organization=self.foreign_org, name="Foreign deadline", created_by=self.foreign_member,
            due_at=due,
        )
        changed_status = self.org.task_statuses.get(code="pending")
        changed_status.name = "Custom task label"
        changed_status.save(update_fields=["name"])

        rows = self.items(source="task_deadline", project_id=self.project.pk, department_id=self.department.pk)
        self.assertCountEqual([row["id"] for row in rows], [parent.pk, child.pk])
        for row in rows:
            self.assertEqual(row["status_label"], changed_status.name)
            self.assertEqual(datetime.fromisoformat(row["starts_at"]), due)
            self.assertEqual(row["url"], reverse("organization-task", args=[self.org.pk, row["id"]]))
            self.assertFalse(row["can_edit"])
        self.assertEqual(self.items(source="meeting"), [])
        events = self.client.get(self.events_url, {"start": "2026-11-09", "end": "2026-11-10"})
        self.assertEqual(events.status_code, 200, events.content)
        self.assertEqual(events.json()["items"], [])
        self.assertEqual(self.items(source="task_deadline", department_id=self.other_department.pk), [])
        self.assertCountEqual(
            [row["id"] for row in self.items(source="task_deadline", include_archived=True)],
            [parent.pk, child.pk, archived.pk],
        )
        moved = write(
            self.client, "patch", f"{self.base}tasks/{child.pk}/",
            {"due_at": "2026-11-10T11:00:00+05:00"},
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual([row["id"] for row in self.items(source="task_deadline")], [parent.pk])
        self.assertEqual([row["id"] for row in self.items("2026-11-10", "2026-11-11")], [child.pk])
        self.assertFalse(CalendarEvent.objects.filter(organization=self.org).exists())

    def test_calendar_does_not_leak_closed_tasks_even_to_calendar_and_task_reader(self):
        due = datetime.fromisoformat("2026-11-09T11:00:00+05:00")
        closed = Task.objects.create(
            organization=self.org, name="Secret project deadline", created_by=self.owner_member,
            due_at=due, project=self.project,
        )
        mine = Task.objects.create(
            organization=self.org, name="Visible assigned deadline", created_by=self.owner_member,
            due_at=due, assignee=self.reader_member,
        )
        hidden = Task.objects.create(
            organization=self.org, name="Hidden standalone deadline", created_by=self.owner_member, due_at=due,
        )
        self.client.force_login(self.reader)
        self.assertEqual([row["id"] for row in self.items(source="task_deadline")], [mine.pk])
        grant(self.org, self.reader_member, "tasks.view")
        rows = self.items(source="task_deadline")
        self.assertCountEqual([row["id"] for row in rows], [mine.pk, hidden.pk])
        self.assertNotIn(closed.name, str(rows))
        grant(self.org, self.reader_member, "projects.view")
        self.assertCountEqual([row["id"] for row in self.items(source="task_deadline")], [
            mine.pk, hidden.pk, closed.pk,
        ])

    def test_tenant_and_session_checks_protect_reads_writes_and_related_ids(self):
        self.assertEqual(self.fetch_calendar().status_code, 401)
        self.assertEqual(self.client.get(f"{self.calendar_url}filters/").status_code, 401)
        self.assertEqual(self.create_event().status_code, 401)
        self.client.force_login(self.owner)
        event = self.make_event()
        self.assertEqual(
            write(self.client, "post", self.events_url, {
                "title": "No CSRF", "type": "meeting",
                "starts_at": event["starts_at"], "ends_at": event["ends_at"], "timezone": "UTC",
            }, csrf=False).status_code,
            403,
        )
        self.assertEqual(
            write(self.client, "patch", self.event_url(event), {"title": "No CSRF"}, csrf=False).status_code,
            403,
        )
        self.assertEqual(
            write(self.client, "post", self.event_url(event, "cancel/"), {}, csrf=False).status_code,
            403,
        )
        for invalid in ({"project_id": self.foreign_project.pk}, {"department_id": self.foreign_department.pk}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.create_event(**invalid).status_code, 404)
                self.assertEqual(write(self.client, "patch", self.event_url(event), invalid).status_code, 404)
        self.assertEqual(self.client.get(self.event_url(event)).json()["title"], event["title"])

        self.client.force_login(self.foreign_owner)
        session = self.client.session
        session["active_workspace_id"] = self.org.pk
        session.save()
        self.assertEqual(self.fetch_calendar().status_code, 404)
        self.assertEqual(self.client.get(self.event_url(event)).status_code, 404)
        self.assertEqual(self.create_event().status_code, 404)
        self.assertEqual(
            write(self.client, "post", self.event_url(event, "archive/"), {}).status_code,
            404,
        )

    def test_department_scope_inherits_read_create_but_cannot_retarget_or_manage(self):
        self.client.force_login(self.owner)
        other = self.make_event("Other branch event", department_id=self.other_department.pk)
        organization_event = self.make_event("Organization event")
        self.client.force_login(self.scoped)
        child = self.make_event("Inherited child event", department_id=self.child_department.pk)
        self.assertEqual(child["department_id"], self.child_department.pk)
        self.assertEqual([row["id"] for row in self.items()], [child["id"]])
        for unavailable in (other, organization_event):
            with self.subTest(event=unavailable["id"]):
                self.assertEqual(self.client.get(self.event_url(unavailable)).status_code, 404)
        self.assertEqual(self.create_event().status_code, 403)
        self.assertEqual(self.create_event(department_id=self.other_department.pk).status_code, 403)
        self.assertEqual(
            write(self.client, "patch", self.event_url(child), {"title": "Creator cannot update"}).status_code,
            403,
        )
        grant(
            self.org, self.scoped_member, "calendar.update",
            department=self.department, descendants=True,
        )
        self.assertEqual(
            write(self.client, "patch", self.event_url(child), {"department_id": self.other_department.pk})
            .status_code, 403,
        )
        self.assertEqual(self.client.get(self.event_url(child)).json()["department_id"], self.child_department.pk)
        filters = self.client.get(f"{self.calendar_url}filters/")
        self.assertEqual(filters.status_code, 200, filters.content)
        data = filters.json()
        self.assertTrue(data["can_create"])
        self.assertFalse(data["can_create_organization"])
        choices = {row["id"]: row["can_create"] for row in data["departments"]}
        self.assertTrue(choices[self.department.pk])
        self.assertTrue(choices[self.child_department.pk])
        self.assertFalse(choices.get(self.other_department.pk, False))

    def test_project_event_requires_both_project_visibility_and_calendar_read(self):
        self.client.force_login(self.owner)
        event = self.make_event("Private project event", project_id=self.project.pk)
        self.assertEqual(event["project_id"], self.project.pk)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.event_url(event)).status_code, 404)
        self.assertEqual(self.items(), [])
        filters = self.client.get(f"{self.calendar_url}filters/").json()
        self.assertNotIn(self.project.pk, [project["id"] for project in filters["projects"]])

        ProjectMembership.objects.create(
            organization=self.org, project=self.project, organization_membership=self.reader_member,
        )
        self.assertEqual(self.client.get(self.event_url(event)).status_code, 200)
        self.assertEqual([row["id"] for row in self.items(project_id=self.project.pk)], [event["id"]])
        self.assertEqual(self.create_event(project_id=self.project.pk).status_code, 403)
        self.client.force_login(self.no_access)
        ProjectMembership.objects.create(
            organization=self.org, project=self.project,
            organization_membership=self.org.memberships.get(user=self.no_access),
        )
        self.assertEqual(self.client.get(self.event_url(event)).status_code, 404)

    def test_database_rejects_foreign_links_and_invalid_intervals_on_direct_write(self):
        self.client.force_login(self.owner)
        event = self.make_event()
        record = CalendarEvent.objects.get(pk=event["id"])
        for invalid in (
            {"department": self.foreign_department},
            {"project": self.foreign_project},
            {"created_by": self.foreign_member},
            {"ends_at": record.starts_at},
        ):
            with self.subTest(fields=list(invalid)), self.assertRaises(IntegrityError), transaction.atomic():
                CalendarEvent.objects.filter(pk=record.pk).update(**invalid)
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        record.refresh_from_db()
        self.assertIsNone(record.department_id)
        self.assertIsNone(record.project_id)
        self.assertEqual(record.created_by_id, self.owner_member.pk)
        self.assertGreater(record.ends_at, record.starts_at)

    def test_calendar_feed_query_count_does_not_grow_per_record(self):
        self.client.force_login(self.owner)
        due = datetime.fromisoformat("2026-11-09T11:00:00+05:00")
        self.make_event("Initial event")
        Task.objects.create(
            organization=self.org, name="Initial deadline", created_by=self.owner_member, due_at=due,
            project=self.project, department=self.department,
        )
        self.assertEqual(len(self.items()), 2)
        with CaptureQueriesContext(connection) as baseline:
            self.assertEqual(len(self.items()), 2)
        for index in range(8):
            self.make_event(f"Meeting {index}", project_id=self.project.pk)
            Task.objects.create(
                organization=self.org, name=f"Deadline {index}", created_by=self.owner_member, due_at=due,
                project=self.project, department=self.department,
            )
        with CaptureQueriesContext(connection) as expanded:
            self.assertEqual(len(self.items()), 18)
        self.assertLessEqual(len(expanded), len(baseline) + 2)

    def test_calendar_page_requires_login_and_active_workspace_access(self):
        url = reverse("organization-calendar", args=[self.org.pk])
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(url).status_code, 404)
        overview = reverse("organization-detail", args=[self.org.pk])
        self.client.force_login(self.no_access)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(overview), f'href="{url}"')
        self.client.force_login(self.owner)
        meeting = self.make_event("Only calendar readers")
        task = Task.objects.create(
            organization=self.org, name="Personal deadline", created_by=self.owner_member,
            assignee=self.org.memberships.get(user=self.no_access),
            due_at=datetime.fromisoformat("2026-11-09T11:00:00+05:00"),
        )
        self.client.force_login(self.no_access)
        self.assertContains(self.client.get(overview), f'href="{url}"')
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            [(row["source"], row["id"]) for row in self.items()], [("task_deadline", task.pk)],
        )
        self.assertEqual(self.client.get(self.event_url(meeting)).status_code, 404)

        self.client.force_login(self.reader)
        self.assertContains(self.client.get(overview), f'href="{url}"')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        markup = CollectionMarkup(response)
        dialog_id = "calendar-editor"
        dialog_tag, dialog = markup.ids[dialog_id]
        self.assertEqual(dialog_tag, "dialog")
        self.assertNotIn("open", dialog)
        self.assertIn(dialog["aria-labelledby"], markup.ids)
        self.assertEqual(markup.ids["calendar-create"][1]["aria-controls"], dialog_id)
        self.assertIn("hidden", markup.ids["calendar-create"][1])
        self.assertIn("calendar-event-title", markup.labels)
        self.assertEqual(markup.forms["calendar-event-form"][0], dialog_id)
        self.assertEqual(markup.ids["calendar-status"][1]["role"], "status")
        self.assertEqual(markup.ids["calendar-error"][1]["role"], "alert")
