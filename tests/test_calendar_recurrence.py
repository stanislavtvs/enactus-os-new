from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase

from apps.calendar_app.models import CalendarEvent
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from tests.test_department_memberships import write
from tests.test_projects import grant


class CalendarRecurrenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="calendar-series-owner")
        cls.foreign_owner = users.create_superuser(username="calendar-series-foreign")
        cls.reader = users.create_user(username="calendar-series-reader")
        cls.org = create_organization(user=cls.owner, name="Calendar recurrence")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign recurrence")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.reader_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.reader)
        grant(cls.org, cls.reader_member, "calendar.read")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.base = f"/api/v1/organizations/{self.org.pk}/"
        self.client.force_login(self.owner)

    def event_url(self, event, action=""):
        return f"{self.base}events/{event.pk}/{action}"

    def event(self, starts_at, ends_at, *, organization=None, creator=None, timezone="UTC", **fields):
        return CalendarEvent.objects.create(
            organization=organization or self.org, created_by=creator or self.owner_member,
            title="Recurring meeting", event_type=CalendarEvent.Type.MEETING,
            starts_at=datetime.fromisoformat(starts_at), ends_at=datetime.fromisoformat(ends_at),
            timezone=timezone, **fields,
        )

    def put_rule(self, event, **fields):
        return write(
            self.client, "put", self.event_url(event, "recurrence/"),
            {"recurrence": {"frequency": "daily", "interval": 1, **fields}},
        )

    def rows(self, start, end, *, timezone="UTC", **filters):
        response = self.client.get(f"{self.base}calendar/", {
            "start": start, "end": end, "timezone": timezone, **filters,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def starts(self, rows, timezone="UTC"):
        zone = ZoneInfo(timezone)
        return [datetime.fromisoformat(row["starts_at"]).astimezone(zone) for row in rows]

    def days(self, rows, timezone="UTC"):
        return [value.date().isoformat() for value in self.starts(rows, timezone)]

    def test_unbounded_daily_interval_generates_only_requested_range_and_no_physical_copies(self):
        event = self.event("2026-11-09T16:00:00+00:00", "2026-11-09T17:00:00+00:00")
        response = self.put_rule(event, interval=2)
        self.assertEqual(response.status_code, 200, response.content)
        first = self.rows("2026-11-09", "2026-11-14")
        self.assertEqual(self.days(first), ["2026-11-09", "2026-11-11", "2026-11-13"])
        narrow = self.rows("2026-11-11", "2026-11-12")
        self.assertEqual(len(narrow), 1)
        self.assertEqual(narrow[0]["occurrence_key"], first[1]["occurrence_key"])
        self.assertEqual(len({row["occurrence_key"] for row in first}), 3)
        self.assertTrue(all(row["series_id"] == event.pk for row in first))

        future = self.rows("2086-11-09", "2086-11-17")
        self.assertEqual(len(future), 4)
        self.assertTrue(all(row["series_id"] == event.pk for row in future))
        self.assertEqual(CalendarEvent.objects.filter(organization=self.org).count(), 1)
        invalid = self.client.get(f"{self.base}calendar/", {"start": "2026-11-09", "end": "2027-01-01"})
        self.assertEqual(invalid.status_code, 422)
        with patch("apps.calendar_app.services.MAX_CALENDAR_ITEMS", 2):
            overflow = self.client.get(f"{self.base}calendar/", {"start": "2026-11-09", "end": "2026-11-14"})
        self.assertEqual(overflow.status_code, 422, overflow.content)

    def test_series_and_single_event_each_keep_their_own_identity(self):
        series = self.event("2026-11-09T16:00:00+00:00", "2026-11-09T17:00:00+00:00")
        single = self.event("2026-11-09T18:00:00+00:00", "2026-11-09T19:00:00+00:00")
        self.assertEqual(self.put_rule(series).status_code, 200)
        rows = self.rows("2026-11-09", "2026-11-10")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["series_id"], series.pk)
        self.assertTrue(rows[0]["occurrence_key"])
        self.assertEqual(rows[1]["id"], single.pk)
        self.assertIsNone(rows[1]["occurrence_key"])

    def test_weekly_interval_with_multiple_days_across_year_and_inclusive_until(self):
        event = self.event("2026-12-28T16:00:00+00:00", "2026-12-28T17:00:00+00:00")
        result = self.put_rule(event, frequency="weekly", interval=2, weekdays=[1, 4], until="2027-01-14")
        self.assertEqual(result.status_code, 200, result.content)
        rows = self.rows("2026-12-27", "2027-01-20")
        self.assertEqual(self.days(rows), ["2026-12-28", "2026-12-31", "2027-01-11", "2027-01-14"])
        self.assertEqual(self.rows("2027-01-14", "2027-01-15")[0]["occurrence_key"], rows[-1]["occurrence_key"])
        saved = self.client.get(self.event_url(event, "recurrence/"))
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertTrue(saved.json()["can_edit"])
        self.assertEqual(saved.json()["recurrence"]["weekdays"], [1, 4])
        self.assertEqual(saved.json()["recurrence"]["until"], "2027-01-14")
        self.assertEqual([row["occurrence_key"] for row in rows], self.days(rows))

    def test_monthly_31st_skips_short_months_without_consuming_count(self):
        event = self.event("2027-01-31T10:00:00+00:00", "2027-01-31T11:00:00+00:00")
        result = self.put_rule(event, frequency="monthly", count=3)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(self.days(self.rows("2027-01-29", "2027-03-03")), ["2027-01-31"])
        self.assertEqual(self.days(self.rows("2027-03-25", "2027-05-01")), ["2027-03-31"])
        self.assertEqual(self.days(self.rows("2027-05-01", "2027-06-01")), ["2027-05-31"])
        self.assertEqual(self.rows("2027-07-01", "2027-08-01"), [])

    def test_yearly_leap_day_skips_non_leap_years(self):
        event = self.event("2024-02-29T12:00:00+00:00", "2024-02-29T13:00:00+00:00")
        result = self.put_rule(event, frequency="yearly", count=2)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(self.rows("2027-02-20", "2027-03-10"), [])
        self.assertEqual(self.days(self.rows("2028-02-20", "2028-03-10")), ["2028-02-29"])
        self.assertEqual(self.rows("2032-02-20", "2032-03-10"), [])

    def test_series_preserves_wall_clock_after_each_dst_change_in_utc_feed(self):
        for anchor, end, bound, expected_offsets in (
            (
                "2026-03-07T10:00:00-05:00", "2026-03-07T11:00:00-05:00",
                ("2026-03-07", "2026-03-10"), [-5, -4, -4],
            ),
            (
                "2026-10-31T10:00:00-04:00", "2026-10-31T11:00:00-04:00",
                ("2026-10-31", "2026-11-03"), [-4, -5, -5],
            ),
        ):
            with self.subTest(bound=bound):
                event = self.event(anchor, end, timezone="America/New_York")
                self.assertEqual(self.put_rule(event, count=3).status_code, 200)
                rows = self.rows(*bound)
                starts = self.starts(rows, "America/New_York")
                self.assertEqual([value.hour for value in starts], [10, 10, 10])
                self.assertEqual([value.utcoffset().total_seconds() / 3600 for value in starts], expected_offsets)

    def test_multiday_all_day_occurrence_overlaps_viewer_range_by_event_civil_dates(self):
        event = self.event(
            "2026-03-07T00:00:00-05:00", "2026-03-09T00:00:00-04:00",
            timezone="America/New_York", all_day=True,
        )
        response = self.put_rule(event, frequency="weekly", weekdays=[6], count=2)
        self.assertEqual(response.status_code, 200, response.content)
        rows = self.rows("2026-03-08", "2026-03-09", timezone="Pacific/Kiritimati")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["all_day"])
        self.assertEqual(self.days(rows, "America/New_York"), ["2026-03-07"])
        self.assertEqual(
            datetime.fromisoformat(rows[0]["ends_at"]) - datetime.fromisoformat(rows[0]["starts_at"]),
            timedelta(hours=47),
        )
        self.assertEqual(self.rows("2026-03-09", "2026-03-10", timezone="Pacific/Kiritimati"), [])
        next_rows = self.rows("2026-03-14", "2026-03-16", timezone="Pacific/Kiritimati")
        self.assertEqual(len(next_rows), 1)
        self.assertEqual(
            datetime.fromisoformat(next_rows[0]["ends_at"]) - datetime.fromisoformat(next_rows[0]["starts_at"]),
            timedelta(hours=48),
        )

    def test_invalid_rules_rejected_without_enabling_recurrence(self):
        event = self.event("2026-11-09T10:00:00+00:00", "2026-11-09T11:00:00+00:00")
        for invalid in (
            {"interval": 0}, {"frequency": "hourly"}, {"count": 0},
            {"count": 2, "until": "2026-11-12"}, {"until": "2026-11-08"},
            {"frequency": "weekly", "weekdays": [8]}, {"weekdays": [1]},
            {"frequency": "weekly", "weekdays": [4]},
        ):
            with self.subTest(rule=invalid):
                response = self.put_rule(event, **invalid)
                self.assertEqual(response.status_code, 422, response.content)
        items = self.rows("2026-11-09", "2026-11-12")
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["occurrence_key"])

    def test_dst_gap_advances_by_gap_and_fold_uses_first_instant(self):
        spring = self.event(
            "2026-03-07T02:30:00-05:00", "2026-03-07T03:30:00-05:00",
            timezone="America/New_York",
        )
        autumn = self.event(
            "2026-10-31T01:30:00-04:00", "2026-10-31T02:30:00-04:00",
            timezone="America/New_York",
        )
        self.assertEqual(self.put_rule(spring, count=3).status_code, 200)
        self.assertEqual(self.put_rule(autumn, count=3).status_code, 200)
        spring_rows = self.rows("2026-03-07", "2026-03-10")
        self.assertEqual(
            [value.strftime("%H:%M %z") for value in self.starts(spring_rows, "America/New_York")],
            ["02:30 -0500", "03:30 -0400", "02:30 -0400"],
        )
        self.assertEqual(
            datetime.fromisoformat(spring_rows[1]["ends_at"]) - datetime.fromisoformat(spring_rows[1]["starts_at"]),
            timedelta(hours=1),
        )
        fold = self.rows("2026-11-01", "2026-11-02")
        self.assertEqual(len(fold), 1)
        self.assertEqual(self.starts(fold)[0].isoformat(), "2026-11-01T05:30:00+00:00")
        self.assertEqual(fold[0]["occurrence_key"], "2026-11-01")

    def test_create_event_with_recurrence_is_atomic_and_visible_only_to_authorized_members(self):
        collection = f"{self.base}events/"
        payload = {
            "title": "Atomic series", "type": "meeting",
            "starts_at": "2026-11-09T10:00:00+00:00", "ends_at": "2026-11-09T11:00:00+00:00",
            "timezone": "UTC",
        }
        invalid = write(self.client, "post", collection, payload | {
            "recurrence": {"frequency": "daily", "until": "2026-11-08"},
        })
        self.assertEqual(invalid.status_code, 422, invalid.content)
        self.assertFalse(CalendarEvent.objects.filter(organization=self.org).exists())
        response = write(self.client, "post", collection, payload | {
            "recurrence": {"frequency": "daily", "count": 2},
        })
        self.assertEqual(response.status_code, 201, response.content)
        event = CalendarEvent.objects.get(pk=response.json()["id"])
        self.assertEqual(event.recurrence_rule.count, 2)

        self.client.force_login(self.reader)
        detail = self.client.get(self.event_url(event, "recurrence/"))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertFalse(detail.json()["can_edit"])
        self.assertEqual(self.put_rule(event).status_code, 403)
        occurrences = self.client.get(self.event_url(event, "occurrences/"), {
            "start": "2026-11-09", "end": "2026-11-13", "timezone": "UTC",
        })
        self.assertEqual(occurrences.status_code, 200, occurrences.content)
        self.assertEqual(self.days(occurrences.json()["items"]), ["2026-11-09", "2026-11-10"])
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.event_url(event, "recurrence/")).status_code, 404)

    def occurrence_url(self, event, key, action=""):
        return self.event_url(event, f"occurrences/{key}/{action}")

    def patch_occurrence(self, event, key, *, version=0, csrf=True, **changes):
        return write(
            self.client, "patch", self.occurrence_url(event, key),
            {"expected_version": version, **changes}, csrf=csrf,
        )

    def test_move_first_occurrence_outside_original_query_keeps_key_and_no_duplicate(self):
        event = self.event("2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00")
        self.assertEqual(self.put_rule(event, count=3).status_code, 200)
        key = "2027-11-15"
        response = self.patch_occurrence(
            event, key, title="Moved meeting", description="Changed agenda",
            starts_at="2027-11-05T14:00:00+00:00", ends_at="2027-11-05T15:30:00+00:00",
            location="Room 2",
        )
        self.assertEqual(response.status_code, 200, response.content)
        moved = response.json()
        self.assertEqual(moved["occurrence_key"], key)
        self.assertEqual(moved["series_id"], event.pk)
        self.assertTrue(moved["is_exception"])
        self.assertEqual(moved["occurrence_version"], 1)
        self.assertEqual(moved["occurrence_rsvp_revision"], 1)
        self.assertEqual(datetime.fromisoformat(moved["original_starts_at"]), event.starts_at)
        self.assertEqual(moved["description"], "Changed agenda")
        self.assertEqual(moved["location"], "Room 2")

        early = self.rows("2027-11-05", "2027-11-06")
        self.assertEqual([(row["occurrence_key"], row["title"]) for row in early], [(key, "Moved meeting")])
        bounded = self.client.get(self.event_url(event, "occurrences/"), {
            "start": "2027-11-05", "end": "2027-11-06",
        })
        self.assertEqual(bounded.status_code, 200, bounded.content)
        self.assertEqual([row["occurrence_key"] for row in bounded.json()["items"]], [key])
        remaining = self.rows("2027-11-15", "2027-11-18")
        self.assertEqual(self.days(remaining), ["2027-11-16", "2027-11-17"])
        self.assertEqual([row["title"] for row in remaining], [event.title, event.title])
        self.assertEqual(self.client.get(self.occurrence_url(event, key)).json()["occurrence_key"], key)
        self.assertEqual(self.client.get(self.occurrence_url(event, "2027-11-05")).status_code, 404)
        exceptions = self.client.get(self.event_url(event, "recurrence/exceptions/"))
        self.assertEqual(exceptions.status_code, 200, exceptions.content)
        self.assertEqual(exceptions.json()["total"], 1)
        self.assertEqual([row["occurrence_key"] for row in exceptions.json()["items"]], [key])
        saved = event.occurrence_exceptions.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            event.occurrence_exceptions.create(organization=self.foreign, occurrence_key="2027-11-16")
        with self.assertRaises(IntegrityError), transaction.atomic():
            event.occurrence_exceptions.filter(pk=saved.pk).update(occurrence_key="2027-11-16")
        self.assertEqual(CalendarEvent.objects.filter(organization=self.org).count(), 1)

    def test_single_cancel_and_series_cancel_keep_earlier_dates_and_filtered_history(self):
        event = self.event("2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00")
        self.assertEqual(self.put_rule(event, count=4).status_code, 200)
        key = "2027-11-16"
        cancelled = write(self.client, "post", self.occurrence_url(event, key, "cancel/"), {
            "expected_version": 0,
        })
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.days(self.rows("2027-11-15", "2027-11-19")), [
            "2027-11-15", "2027-11-17", "2027-11-18",
        ])
        include = self.rows("2027-11-15", "2027-11-19", include_cancelled=True)
        self.assertEqual([row["status"] for row in include], [
            "scheduled", "cancelled", "scheduled", "scheduled",
        ])
        now = datetime.fromisoformat("2027-11-17T12:00:00+00:00")
        with patch("apps.calendar_app.services.timezone") as clock:
            clock.now.return_value = now
            series = write(self.client, "post", self.event_url(event, "cancel/"))
        self.assertEqual(series.status_code, 200, series.content)
        visible = self.rows("2027-11-15", "2027-11-19")
        self.assertEqual(self.days(visible), ["2027-11-15"])
        include = self.rows("2027-11-15", "2027-11-19", include_cancelled=True)
        self.assertEqual([row["status"] for row in include], [
            "scheduled", "cancelled", "cancelled", "cancelled",
        ])
        self.assertTrue(CalendarEvent.objects.filter(pk=event.pk).exists())

    def test_stop_future_is_exclusive_without_deleting_earlier_occurrences(self):
        event = self.event("2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00")
        self.assertEqual(self.put_rule(event).status_code, 200)
        stopped = write(
            self.client, "post", self.event_url(event, "recurrence/stop/"), {"from_date": "2027-11-17"},
        )
        self.assertEqual(stopped.status_code, 200, stopped.content)
        self.assertEqual(stopped.json()["recurrence"]["stopped_from"], "2027-11-17")
        rows = self.rows("2027-11-15", "2027-11-19", include_cancelled=True)
        self.assertEqual(self.days(rows), ["2027-11-15", "2027-11-16"])
        self.assertEqual(rows[0]["recurrence"]["stopped_from"], "2027-11-17")
        self.assertEqual(self.client.get(self.occurrence_url(event, "2027-11-17")).status_code, 404)
        event.refresh_from_db()
        self.assertEqual(event.status, CalendarEvent.Status.SCHEDULED)

    def test_rule_edits_preserve_saved_exceptions_and_reject_orphaning_history(self):
        event = self.event("2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00")
        self.assertEqual(self.put_rule(event, count=3).status_code, 200)
        key = "2027-11-17"
        changed = self.patch_occurrence(event, key, title="Changed one date")
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.json()["occurrence_rsvp_revision"], 0)
        self.assertEqual(self.put_rule(event, count=2).status_code, 409)
        self.assertEqual(
            write(self.client, "put", self.event_url(event, "recurrence/"), {"recurrence": None}).status_code,
            409,
        )
        self.assertEqual(write(self.client, "patch", self.event_url(event), {
            "starts_at": "2027-11-15T15:00:00+00:00",
        }).status_code, 409)
        self.assertEqual(self.put_rule(event, count=4).status_code, 200)
        occurrence = self.client.get(self.occurrence_url(event, key))
        self.assertEqual(occurrence.status_code, 200, occurrence.content)
        self.assertEqual(occurrence.json()["title"], "Changed one date")
        self.assertEqual(self.days(self.rows("2027-11-15", "2027-11-19")), [
            "2027-11-15", "2027-11-16", "2027-11-17", "2027-11-18",
        ])

    def test_occurrence_mutations_reject_stale_version_csrf_scope_fields_and_idor(self):
        event = self.event("2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00")
        self.assertEqual(self.put_rule(event, count=2).status_code, 200)
        key = "2027-11-15"
        self.assertEqual(self.patch_occurrence(event, key, title="CSRF", csrf=False).status_code, 403)
        self.assertEqual(self.patch_occurrence(event, key, project_id=1).status_code, 422)
        self.assertEqual(self.patch_occurrence(event, "2027-11-20", title="Missing").status_code, 404)
        self.assertEqual(self.patch_occurrence(event, key, title="First edit").status_code, 200)
        self.assertEqual(self.patch_occurrence(event, key, title="Stale edit").status_code, 409)
        self.client.force_login(self.reader)
        self.assertEqual(self.patch_occurrence(event, key, version=1, title="Forbidden").status_code, 403)
        detail = self.client.get(self.occurrence_url(event, key))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["title"], "First edit")
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.occurrence_url(event, key)).status_code, 404)
        self.assertEqual(self.patch_occurrence(event, key, version=1, title="Foreign").status_code, 404)

    def test_invite_only_occurrence_redacts_exception_description_and_denies_edit(self):
        user = get_user_model().objects.create_user(username="calendar-series-invite-only")
        recipient = OrganizationMembership.objects.create(organization=self.org, user=user)
        event = self.event(
            "2027-11-15T16:00:00+00:00", "2027-11-15T17:00:00+00:00",
            description="Confidential base agenda",
        )
        self.assertEqual(self.put_rule(event, count=2).status_code, 200)
        key = "2027-11-15"
        changed = self.patch_occurrence(event, key, description="Confidential exception agenda")
        self.assertEqual(changed.status_code, 200, changed.content)
        sent = write(self.client, "post", self.event_url(event, "invitations/"), {
            "member_ids": [recipient.pk],
        })
        self.assertEqual(sent.status_code, 201, sent.content)
        self.client.force_login(user)
        detail = self.client.get(self.occurrence_url(event, key))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["description"], "")
        self.assertFalse(detail.json()["can_edit"])
        self.assertFalse(detail.json()["can_cancel"])
        self.assertEqual(self.rows("2027-11-15", "2027-11-16")[0]["description"], "")
        self.assertEqual(self.patch_occurrence(event, key, version=1, title="Denied").status_code, 404)


class ConcurrentCalendarOccurrenceTests(TransactionTestCase):
    def test_competing_edits_to_one_occurrence_have_one_winner(self):
        owner = get_user_model().objects.create_superuser(username="occurrence-edit-race-owner")
        org = create_organization(user=owner, name="Occurrence edit race")
        client = Client(enforce_csrf_checks=True)
        client.force_login(owner)
        response = write(client, "post", f"/api/v1/organizations/{org.pk}/events/", {
            "title": "Race series", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
            "recurrence": {"frequency": "daily", "count": 2},
        })
        self.assertEqual(response.status_code, 201, response.content)
        event_id = response.json()["id"]
        url = f"/api/v1/organizations/{org.pk}/events/{event_id}/occurrences/2027-11-15/"
        start = Barrier(2, timeout=8)

        def change(title):
            worker_connection = connections["default"]
            try:
                worker = Client(enforce_csrf_checks=True)
                worker.force_login(owner)
                with worker_connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                start.wait()
                response = write(worker, "patch", url, {"title": title, "expected_version": 0})
                return response.status_code, response.json()
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, title) for title in ("First editor", "Second editor")]
            results = [future.result(timeout=16) for future in futures]
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        saved = client.get(url).json()
        self.assertEqual(saved["title"], winner["title"])
        self.assertEqual(saved["occurrence_version"], 1)
        self.assertEqual(CalendarEvent.objects.get(pk=event_id).occurrence_exceptions.count(), 1)
