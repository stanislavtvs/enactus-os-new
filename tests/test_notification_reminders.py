from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase

from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department
from apps.notifications.models import Notification, NotificationEvent, OrganizationNotificationPolicy
from apps.notifications.reminders import run_reminders
from apps.notifications.services import publish, retry_due
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.tasks.models import Task, TaskCollaborator
from tests.test_department_memberships import write
from tests.test_projects import grant


NOW = datetime.fromisoformat("2027-11-09T12:00:00+00:00")


class NotificationReminderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="reminder-owner")
        cls.worker = users.create_user(username="reminder-worker")
        cls.collaborator = users.create_user(username="reminder-collaborator")
        cls.other = users.create_user(username="reminder-other")
        cls.org = create_organization(user=cls.owner, name="Notification reminders")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.worker_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.worker)
        cls.collaborator_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.collaborator,
        )
        cls.other_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.other)
        cls.department = Department.objects.create(organization=cls.org, name="Reminder department")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.base = f"/api/v1/organizations/{self.org.pk}/"

    def policy(self, **fields):
        return OrganizationNotificationPolicy.objects.update_or_create(organization=self.org, defaults=fields)[0]

    def task(self, due_at, **fields):
        return Task.objects.create(
            organization=self.org, created_by=self.owner_member, name="Reminder task", due_at=due_at,
            **({"assignee": self.worker_member} | fields),
        )

    def calendar(self, starts_at, ends_at, **fields):
        response = write(self.client, "post", f"{self.base}events/", {
            "title": "Reminder event", "type": "meeting", "timezone": "UTC",
            "starts_at": starts_at, "ends_at": ends_at, **fields,
        })
        self.assertEqual(response.status_code, 201, response.content)
        return CalendarEvent.objects.get(pk=response.json()["id"])

    def invite(self, event, membership=None):
        invitation = event.invitations.create(
            organization=self.org, recipient=membership or self.worker_member, invited_by=self.owner_member,
        )
        return invitation

    def rows(self, target, kind):
        return Notification.objects.filter(
            organization=self.org, event__kind=kind, event__object_id=target.pk,
        ).select_related("event")

    def recipients(self, target, kind):
        return list(self.rows(target, kind).values_list("recipient_id", flat=True))

    def run_scan(self, now=NOW):
        return run_reminders(now=now, limit=100)

    def test_calendar_fanout_resumes_across_batches_without_duplicate_delivery(self):
        self.policy(calendar_offsets_minutes=[60])
        event = self.calendar("2027-11-09T13:00:00+00:00", "2027-11-09T14:00:00+00:00")
        for member in (self.worker_member, self.collaborator_member, self.other_member):
            self.invite(event, member)

        for expected in (1, 2, 3, 3):
            run_reminders(now=NOW, limit=1)
            self.assertEqual(self.rows(event, "calendar.reminder").count(), expected)
        self.assertCountEqual(
            self.recipients(event, "calendar.reminder"),
            [self.worker.pk, self.collaborator.pk, self.other.pk],
        )

    def preference(self, user, **fields):
        self.client.force_login(user)
        response = write(self.client, "patch", f"{self.base}notification-preferences/", fields)
        self.assertEqual(response.status_code, 200, response.content)
        self.client.force_login(self.owner)

    def test_task_offsets_and_subtask_link_are_deduplicated_per_recipient(self):
        self.policy(task_offsets_minutes=[1440, 60], include_collaborators=True)
        parent = self.task(None)
        task = self.task(NOW + timedelta(days=1), parent=parent)
        TaskCollaborator.objects.create(
            organization=self.org, task=task, organization_membership=self.collaborator_member,
        )
        self.run_scan(NOW - timedelta(seconds=1))
        self.assertEqual(self.recipients(task, "task.reminder"), [])
        self.run_scan()
        self.run_scan()
        self.assertCountEqual(self.recipients(task, "task.reminder"), [self.worker.pk, self.collaborator.pk])
        self.run_scan(NOW + timedelta(hours=23))
        self.run_scan(NOW + timedelta(hours=23))
        rows = self.rows(task, "task.reminder")
        self.assertEqual(rows.count(), 4)
        self.assertCountEqual([row.event.data["offset_minutes"] for row in rows], [1440, 1440, 60, 60])
        self.assertEqual(self.recipients(parent, "task.reminder"), [])
        notice = rows.filter(recipient=self.worker).first()
        self.client.force_login(self.worker)
        link = self.client.get(f"/api/v1/notifications/{notice.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertTrue(link.json()["url"].endswith(f"/tasks/{task.pk}/"))

    def test_overdue_is_once_and_neither_old_nor_terminal_tasks_emit(self):
        self.policy(task_offsets_minutes=[0], task_overdue=True)
        live = self.task(NOW - timedelta(minutes=2))
        past = self.task(NOW - timedelta(days=2))
        cancelled = self.task(NOW - timedelta(minutes=2), status=Task.Status.CANCELLED)
        archived = self.task(NOW - timedelta(minutes=2), is_archived=True)
        no_assignee = self.task(NOW - timedelta(minutes=2), assignee=None)
        for _ in range(2):
            self.run_scan()
        self.assertEqual(self.recipients(live, "task.overdue"), [self.worker.pk])
        self.assertEqual(self.recipients(live, "task.reminder"), [self.worker.pk])
        for task in (past, cancelled, archived, no_assignee):
            with self.subTest(task=task.pk):
                self.assertEqual(self.recipients(task, "task.overdue"), [])
                self.assertEqual(self.recipients(task, "task.reminder"), [])
        live.refresh_from_db()
        self.assertEqual(live.status, Task.Status.PENDING)

    def test_task_due_change_prevents_stale_retry_and_new_due_can_send(self):
        self.policy(task_offsets_minutes=[60])
        task = self.task(NOW + timedelta(hours=1))
        with patch("apps.notifications.services._deliver_notifications", side_effect=IntegrityError("temporary")):
            self.run_scan()
        old = NotificationEvent.objects.get(kind="task.reminder", object_id=task.pk)
        self.assertIsNone(old.delivered_at)
        self.assertEqual(old.delivery_attempts, 1)
        Task.objects.filter(pk=task.pk).update(due_at=NOW + timedelta(hours=2))
        retry_due(now=NOW + timedelta(minutes=1))
        self.assertEqual(self.recipients(task, "task.reminder"), [])
        self.run_scan(NOW + timedelta(hours=1))
        self.assertEqual(self.recipients(task, "task.reminder"), [self.worker.pk])
        self.assertEqual(self.rows(task, "task.reminder").get().event.data["offset_minutes"], 60)

    def test_calendar_excludes_declined_and_revoked_and_reminds_before_multiday_start(self):
        self.policy(calendar_offsets_minutes=[60])
        event = self.calendar(
            "2027-11-09T13:00:00+00:00", "2027-11-11T13:00:00+00:00", rsvp_mode="optional",
        )
        self.invite(event)
        declined = self.invite(event, self.collaborator_member)
        revoked = self.invite(event, self.other_member)
        self.client.force_login(self.collaborator)
        response = write(
            self.client, "post", f"/api/v1/organizations/calendar/invitations/{declined.pk}/respond/",
            {"response": "no", "expected_revision": event.rsvp_revision, "expected_response_at": None},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.client.force_login(self.owner)
        revoke = write(self.client, "post", f"{self.base}events/{event.pk}/invitations/{revoked.pk}/revoke/")
        self.assertEqual(revoke.status_code, 204, revoke.content)
        self.run_scan()
        self.run_scan()
        self.assertEqual(self.recipients(event, "calendar.reminder"), [self.worker.pk])

    def test_recurrence_move_keeps_original_key_and_cancelled_occurrences_never_send(self):
        self.policy(calendar_offsets_minutes=[60])
        event = self.calendar(
            "2027-11-09T13:00:00+00:00", "2027-11-09T14:00:00+00:00",
            recurrence={"frequency": "daily", "count": 3}, rsvp_mode="optional",
        )
        invitation = self.invite(event)
        cancelled_series = self.calendar(
            "2027-11-09T13:00:00+00:00", "2027-11-09T14:00:00+00:00",
            recurrence={"frequency": "daily"},
        )
        self.invite(cancelled_series)
        response = write(self.client, "post", f"{self.base}events/{cancelled_series.pk}/cancel/")
        self.assertEqual(response.status_code, 200, response.content)
        self.client.force_login(self.worker)
        declined = write(
            self.client, "post", f"/api/v1/organizations/calendar/invitations/{invitation.pk}/respond/",
            {"response": "no", "expected_revision": event.rsvp_revision, "expected_response_at": None},
        )
        self.assertEqual(declined.status_code, 200, declined.content)
        self.client.force_login(self.owner)
        first = f"{self.base}events/{event.pk}/occurrences/2027-11-09/"
        moved = write(self.client, "patch", first, {
            "expected_version": 0, "starts_at": "2027-11-09T15:00:00+00:00",
            "ends_at": "2027-11-09T16:00:00+00:00",
        })
        self.assertEqual(moved.status_code, 200, moved.content)
        cancelled = write(
            self.client, "post", f"{self.base}events/{event.pk}/occurrences/2027-11-10/cancel/",
            {"expected_version": 0},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)

        self.run_scan()
        self.assertEqual(self.recipients(event, "calendar.reminder"), [])
        self.assertEqual(self.recipients(cancelled_series, "calendar.reminder"), [])
        self.run_scan(NOW + timedelta(hours=2))
        self.run_scan(NOW + timedelta(hours=2))
        self.run_scan(NOW + timedelta(days=1))
        reminder = self.rows(event, "calendar.reminder").get()
        self.assertEqual(reminder.event.data["occurrence_key"], "2027-11-09")
        self.assertEqual(
            datetime.fromisoformat(reminder.event.data["reference_at"]),
            NOW + timedelta(hours=3),
        )
        self.client.force_login(self.worker)
        link = self.client.get(f"/api/v1/notifications/{reminder.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertTrue(link.json()["url"].endswith("?occurrence=2027-11-09"))

    def test_all_day_reminder_uses_local_time_on_each_side_of_dst(self):
        self.policy(calendar_offsets_minutes=[1440])
        examples = (
            (
                "2027-03-14T00:00:00-05:00", "2027-03-15T00:00:00-04:00",
                "2027-03-13T14:00:00+00:00",
            ),
            (
                "2027-11-07T00:00:00-04:00", "2027-11-08T00:00:00-05:00",
                "2027-11-06T13:00:00+00:00",
            ),
        )
        for starts_at, ends_at, at in examples:
            with self.subTest(start=starts_at):
                event = self.calendar(starts_at, ends_at, timezone="America/New_York", all_day=True)
                self.invite(event)
                instant = datetime.fromisoformat(at)
                self.run_scan(instant - timedelta(seconds=1))
                self.assertEqual(self.recipients(event, "calendar.reminder"), [])
                self.run_scan(instant)
                reminder = self.rows(event, "calendar.reminder").get()
                self.assertEqual(datetime.fromisoformat(reminder.event.data["planned_at"]), instant)

    def test_disabled_categories_and_information_setting_suppress_only_reminder_delivery(self):
        self.policy(task_offsets_minutes=[60], calendar_offsets_minutes=[60], calendar_informational=False)
        event = self.calendar("2027-11-09T13:00:00+00:00", "2027-11-09T14:00:00+00:00")
        self.invite(event)
        task = self.task(NOW + timedelta(hours=1))
        self.policy(enabled_categories=["calendar"])
        self.run_scan()
        self.assertFalse(self.rows(task, "task.reminder").exists())
        self.assertFalse(self.rows(event, "calendar.reminder").exists())
        self.assertTrue(event.invitations.exists())
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.worker_member.pk)

    def test_completed_task_is_skipped_without_modifying_its_review_history(self):
        self.policy(task_offsets_minutes=[60])
        task = self.task(NOW + timedelta(hours=1), department=self.department, status=Task.Status.IN_PROGRESS)
        grant(self.org, self.worker_member, "tasks.review.submit")
        self.client.force_login(self.worker)
        submission = write(self.client, "post", f"{self.base}tasks/{task.pk}/review/submissions/", {
            "comment": "Completed before reminder", "links": [],
        })
        self.assertEqual(submission.status_code, 204, submission.content)
        stage = task.review_submissions.get().stages.get()
        self.client.force_login(self.owner)
        approved = write(self.client, "post", f"{self.base}tasks/{task.pk}/review/decisions/", {
            "stage_id": stage.pk, "outcome": "approved", "comment": "",
        })
        self.assertEqual(approved.status_code, 204, approved.content)
        self.run_scan()
        self.assertEqual(self.recipients(task, "task.reminder"), [])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(task.review_submissions.count(), 1)

    def test_quiet_hours_defer_until_allowed_and_skip_expired_reminders(self):
        self.policy(task_offsets_minutes=[180, 60])
        self.preference(self.worker, timezone="UTC", quiet_start="22:00", quiet_end="00:30")
        now = datetime.fromisoformat("2027-11-09T22:15:00+00:00")
        due_later = self.task(now + timedelta(hours=3))
        expires_in_quiet = self.task(now + timedelta(hours=1))
        self.run_scan(now)
        delayed = NotificationEvent.objects.get(kind="task.reminder", object_id=due_later.pk)
        resumes = datetime.fromisoformat("2027-11-10T00:30:00+00:00")
        self.assertEqual(delayed.next_attempt_at, resumes)
        self.assertEqual(datetime.fromisoformat(delayed.data["planned_at"]), now)
        self.assertEqual(delayed.delivery_attempts, 0)
        self.assertFalse(delayed.notifications.exists())
        self.assertFalse(NotificationEvent.objects.filter(kind="task.reminder", object_id=expires_in_quiet.pk).exists())
        self.assertEqual(retry_due(now=resumes - timedelta(seconds=1))["processed"], 0)
        self.assertEqual(retry_due(now=resumes)["delivered"], 1)
        self.assertEqual(self.recipients(due_later, "task.reminder"), [self.worker.pk])

    def test_quiet_hours_cover_both_folds_and_move_past_spring_gap(self):
        self.policy(task_offsets_minutes=[180, 60])
        self.preference(self.worker, timezone="America/New_York", quiet_start="22:00", quiet_end="02:30")
        spring = datetime.fromisoformat("2027-03-14T05:30:00+00:00")
        spring_task = self.task(spring + timedelta(hours=3))
        self.run_scan(spring)
        spring_event = NotificationEvent.objects.get(kind="task.reminder", object_id=spring_task.pk)
        resume = datetime.fromisoformat("2027-03-14T07:30:00+00:00")
        self.assertEqual(spring_event.next_attempt_at, resume)
        self.assertEqual(retry_due(now=resume)["delivered"], 1)

        self.preference(self.worker, quiet_end="01:30")
        release = datetime.fromisoformat("2027-11-07T06:30:00+00:00")
        for start in ("2027-11-07T05:10:00+00:00", "2027-11-07T06:10:00+00:00"):
            with self.subTest(now=start):
                now = datetime.fromisoformat(start)
                task = self.task(now + timedelta(hours=3))
                self.run_scan(now)
                event = NotificationEvent.objects.get(kind="task.reminder", object_id=task.pk)
                self.assertEqual(event.next_attempt_at, release)
        self.assertEqual(retry_due(now=release - timedelta(seconds=1))["processed"], 0)
        self.assertEqual(retry_due(now=release)["delivered"], 2)

    def test_catchup_does_not_send_deadline_reminder_missed_in_quiet_hours(self):
        self.policy(task_offsets_minutes=[0])
        self.preference(self.worker, quiet_start="22:00", quiet_end="08:00")
        deadline = datetime.fromisoformat("2027-11-10T07:45:00+00:00")
        task = self.task(deadline)
        self.run_scan(deadline + timedelta(minutes=20))
        self.assertFalse(self.rows(task, "task.reminder").exists())

    def test_per_user_reminder_opt_out_does_not_mute_collaborator_or_business_delivery(self):
        self.policy(task_offsets_minutes=[60], include_collaborators=True)
        self.preference(
            self.worker, task_reminders=False, timezone="UTC", quiet_start="11:00", quiet_end="13:00",
        )
        task = self.task(NOW + timedelta(hours=1))
        TaskCollaborator.objects.create(
            organization=self.org, task=task, organization_membership=self.collaborator_member,
        )
        self.run_scan()
        self.assertEqual(self.recipients(task, "task.reminder"), [self.collaborator.pk])
        event = publish(
            organization_id=self.org.pk, actor_id=self.owner.pk, kind="task.assigned",
            object_type="task", object_id=task.pk, recipient_ids=[self.worker.pk],
            key="test:quiet-business", now=NOW,
        )
        self.assertEqual(event.delivered_at, NOW)
        self.assertEqual(self.recipients(task, "task.assigned"), [self.worker.pk])
