from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.calendar_app import upcoming
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from tests.test_department_memberships import write


class CalendarUpcomingInvitationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="upcoming-series-owner")
        cls.recipient = users.create_user(username="upcoming-series-recipient")
        cls.org = create_organization(user=cls.owner, name="Upcoming series")
        cls.member = OrganizationMembership.objects.create(organization=cls.org, user=cls.recipient)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.events = f"/api/v1/organizations/{self.org.pk}/events/"
        self.inbox = "/api/v1/organizations/calendar/invitations/"
        self.client.force_login(self.owner)

    def invite(self, recurrence=None, **fields):
        response = write(self.client, "post", self.events, {
            "title": "Upcoming event", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
            "recurrence": recurrence, **fields,
        })
        self.assertEqual(response.status_code, 201, response.content)
        event = response.json()
        sent = write(self.client, "post", f"{self.events}{event['id']}/invitations/", {
            "member_ids": [self.member.pk],
        })
        self.assertEqual(sent.status_code, 201, sent.content)
        return event

    def rows_at(self, now, **query):
        self.client.force_login(self.recipient)
        with patch("apps.calendar_app.upcoming.django_timezone") as clock:
            clock.now.return_value = datetime.fromisoformat(now)
            response = self.client.get(self.inbox, {"org_id": self.org.pk, **query})
        self.assertEqual(response.status_code, 200, response.content)
        return {row["event_id"]: row for row in response.json()["items"]}

    def test_finite_series_ends_at_last_occurrence_but_remains_in_history(self):
        event = self.invite({"frequency": "daily", "count": 3})
        active = self.rows_at("2027-11-17T16:59:00+00:00")
        self.assertTrue(active[event["id"]]["is_upcoming"])
        ended = self.rows_at("2027-11-17T17:00:00+00:00")
        self.assertFalse(ended[event["id"]]["is_upcoming"])
        self.assertEqual(ended[event["id"]]["event"]["title"], event["title"])

    def test_cancelling_last_remaining_occurrence_removes_series_from_upcoming(self):
        event = self.invite({"frequency": "daily", "count": 3})
        key = "2027-11-17"
        cancelled = write(self.client, "post", f"{self.events}{event['id']}/occurrences/{key}/cancel/", {
            "expected_version": 0,
        })
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        items = self.rows_at("2027-11-17T12:00:00+00:00")
        self.assertFalse(items[event["id"]]["is_upcoming"])

    def test_moved_occurrence_after_original_last_date_keeps_series_upcoming(self):
        event = self.invite({"frequency": "daily", "count": 2})
        key = "2027-11-16"
        moved = write(self.client, "patch", f"{self.events}{event['id']}/occurrences/{key}/", {
            "expected_version": 0, "starts_at": "2027-12-05T16:00:00+00:00",
            "ends_at": "2027-12-05T17:00:00+00:00",
        })
        self.assertEqual(moved.status_code, 200, moved.content)
        before = self.rows_at("2027-11-30T10:00:00+00:00")
        self.assertTrue(before[event["id"]]["is_upcoming"])
        after = self.rows_at("2027-12-05T17:00:00+00:00")
        self.assertFalse(after[event["id"]]["is_upcoming"])

    def test_all_day_series_uses_viewer_civil_date_at_exclusive_end(self):
        event = self.invite(
            {"frequency": "daily", "count": 2}, all_day=True, timezone="America/New_York",
            starts_at="2027-11-15T00:00:00-05:00", ends_at="2027-11-16T00:00:00-05:00",
        )
        now = "2027-11-16T12:00:00+00:00"
        west = self.rows_at(now, timezone="America/Los_Angeles")
        east = self.rows_at(now, timezone="Pacific/Kiritimati")
        self.assertTrue(west[event["id"]]["is_upcoming"])
        self.assertFalse(east[event["id"]]["is_upcoming"])
        invalid = self.client.get(self.inbox, {"timezone": "Invalid/Timezone"})
        self.assertEqual(invalid.status_code, 422)

    def test_unbounded_old_series_checks_near_viewer_date_without_materializing_history(self):
        event = self.invite(
            {"frequency": "daily"}, starts_at="2000-01-01T16:00:00+00:00",
            ends_at="2000-01-01T17:00:00+00:00",
        )
        with patch("apps.calendar_app.upcoming._occurrence", wraps=upcoming._occurrence) as resolve:
            result = self.rows_at("2080-11-16T12:00:00+00:00")
        self.assertTrue(result[event["id"]]["is_upcoming"])
        self.assertLessEqual(resolve.call_count, 4)
