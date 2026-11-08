from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from apps.calendar_app.models import CalendarEvent, CalendarOccurrenceRSVP, CalendarOccurrenceRSVPHistory
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from tests.test_department_memberships import write


class CalendarOccurrenceRSVPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="occurrence-rsvp-owner")
        cls.recipient_user = users.create_user(username="occurrence-rsvp-recipient")
        cls.other_user = users.create_user(username="occurrence-rsvp-other")
        cls.foreign_owner = users.create_superuser(username="occurrence-rsvp-foreign")
        cls.organization = create_organization(user=cls.owner, name="Occurrence RSVP")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign occurrence RSVP")
        cls.recipient = OrganizationMembership.objects.create(organization=cls.organization, user=cls.recipient_user)
        cls.other = OrganizationMembership.objects.create(organization=cls.organization, user=cls.other_user)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.base = f"/api/v1/organizations/{self.organization.pk}/events/"
        self.inbox = "/api/v1/organizations/calendar/invitations/"
        self.first = "2027-11-15"
        self.second = "2027-11-16"
        self.client.force_login(self.owner)

    def create_series(self, *, mode="required", count=3):
        response = write(self.client, "post", self.base, {
            "title": "RSVP series", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
            "rsvp_mode": mode, "recurrence": {"frequency": "daily", "count": count},
        })
        self.assertEqual(response.status_code, 201, response.content)
        return CalendarEvent.objects.get(pk=response.json()["id"])

    def send(self, event, **audience):
        response = write(self.client, "post", f"{self.base}{event.pk}/invitations/", audience)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def invite(self, event, membership=None):
        membership = membership or self.recipient
        self.send(event, member_ids=[membership.pk])
        return event.invitations.get(recipient=membership)

    def own_url(self, invitation, key=None, action=""):
        base = f"{self.inbox}{invitation.pk}/"
        if key is not None:
            base += f"occurrences/{key}/"
        return base + action

    def own(self, invitation, key=None):
        response = self.client.get(self.own_url(invitation, key))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def reply(self, invitation, response, *, revision, key=None, occurrence_revision=0, responded_at=None, csrf=True):
        data = {"response": response, "expected_revision": revision, "expected_response_at": responded_at}
        if key is not None:
            data["expected_occurrence_revision"] = occurrence_revision
        return write(self.client, "post", self.own_url(invitation, key, "respond/"), data, csrf=csrf)

    def change_occurrence(self, event, key, action="", *, version=0, **fields):
        return write(
            self.client, "post" if action else "patch", f"{self.base}{event.pk}/occurrences/{key}/{action}",
            {"expected_version": version, **fields},
        )

    def test_series_answer_is_inherited_and_single_decline_does_not_overwrite_it(self):
        event = self.create_series()
        invitation = self.invite(event)
        self.client.force_login(self.recipient_user)
        initial = self.own(invitation, self.first)
        self.assertIsNone(initial["response"])
        self.assertEqual(initial["occurrence_rsvp_revision"], 0)
        self.assertIsNone(initial["responded_at"])
        self.assertEqual(self.reply(invitation, "yes", revision=event.rsvp_revision).status_code, 200)

        inherited = self.own(invitation, self.first)
        self.assertEqual(inherited["series_response"], "yes")
        self.assertEqual(inherited["response"], "yes")
        self.assertIsNone(inherited["responded_at"])
        declined = self.reply(invitation, "no", revision=event.rsvp_revision, key=self.first)
        self.assertEqual(declined.status_code, 200, declined.content)
        self.assertEqual(declined.json()["response"], "no")
        self.assertEqual(declined.json()["series_response"], "yes")
        self.assertIsNotNone(declined.json()["responded_at"])
        self.assertEqual(self.own(invitation, self.second)["response"], "yes")
        self.assertEqual(self.own(invitation)["response"], "yes")
        invitation.refresh_from_db()
        self.assertEqual(invitation.response, "yes")
        self.assertEqual(event.invitations.count(), 1)
        self.assertEqual(CalendarEvent.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(CalendarOccurrenceRSVP.objects.filter(event=event).count(), 1)

    def test_moved_occurrence_requires_reconfirmation_and_preserves_sister_responses(self):
        event = self.create_series()
        invitation = self.invite(event)
        self.client.force_login(self.recipient_user)
        self.assertEqual(self.reply(invitation, "yes", revision=event.rsvp_revision).status_code, 200)
        previous = self.reply(invitation, "maybe", revision=event.rsvp_revision, key=self.first)
        self.assertEqual(previous.status_code, 200, previous.content)
        previous_at = previous.json()["responded_at"]

        self.client.force_login(self.owner)
        moved = self.change_occurrence(
            event, self.first, starts_at="2027-11-15T19:00:00+00:00", ends_at="2027-11-15T20:00:00+00:00",
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual(moved.json()["occurrence_rsvp_revision"], 1)

        self.client.force_login(self.recipient_user)
        changed = self.own(invitation, self.first)
        self.assertIsNone(changed["response"])
        self.assertEqual(changed["series_response"], "yes")
        self.assertEqual(changed["occurrence_key"], self.first)
        self.assertEqual(changed["occurrence_rsvp_revision"], 1)
        self.assertTrue(changed["needs_reconfirmation"])
        self.assertEqual(changed["responded_at"], previous_at)
        self.assertEqual(self.own(invitation, self.second)["response"], "yes")
        self.assertEqual(self.reply(
            invitation, "no", revision=event.rsvp_revision, key=self.first, responded_at=previous_at,
        ).status_code, 409)
        confirmed = self.reply(
            invitation, "no", revision=event.rsvp_revision, key=self.first,
            occurrence_revision=1, responded_at=previous_at,
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertEqual(confirmed.json()["response"], "no")
        self.assertFalse(confirmed.json()["needs_reconfirmation"])
        self.assertEqual(self.own(invitation)["response"], "yes")
        history_url = f"{self.base}{event.pk}/occurrences/{self.first}/invitations/{invitation.pk}/history/"
        self.assertEqual(self.client.get(history_url).status_code, 404)
        self.client.force_login(self.owner)
        history = self.client.get(history_url)
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual([row["response"] for row in history.json()["items"]], ["maybe", "no"])
        self.assertEqual(
            list(CalendarOccurrenceRSVPHistory.objects.filter(
                invitation=invitation, occurrence_key=self.first,
            ).order_by("pk").values_list("response", "occurrence_rsvp_revision")),
            [("maybe", 0), ("no", 1)],
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CalendarOccurrenceRSVPHistory.objects.filter(invitation=invitation).update(response="yes")

    def test_moved_occurrence_does_not_inherit_prior_base_answer_without_override(self):
        event = self.create_series()
        invitation = self.invite(event)
        self.client.force_login(self.recipient_user)
        self.assertEqual(self.reply(invitation, "yes", revision=event.rsvp_revision).status_code, 200)
        self.client.force_login(self.owner)
        moved = self.change_occurrence(
            event, self.second, starts_at="2027-11-17T19:00:00+00:00", ends_at="2027-11-17T20:00:00+00:00",
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.client.force_login(self.recipient_user)
        changed = self.own(invitation, self.second)
        self.assertIsNone(changed["response"])
        self.assertEqual(changed["series_response"], "yes")
        self.assertTrue(changed["needs_reconfirmation"])
        self.assertIsNone(changed["responded_at"])
        self.assertEqual(self.own(invitation, self.first)["response"], "yes")

    def test_own_occurrence_reply_requires_matching_tenant_csrf_and_current_timestamp(self):
        event = self.create_series()
        invitation = self.invite(event)
        for actor in (self.owner, self.other_user, self.foreign_owner):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                self.assertEqual(self.client.get(self.own_url(invitation, self.first)).status_code, 404)
                self.assertEqual(self.reply(
                    invitation, "yes", revision=event.rsvp_revision, key=self.first,
                ).status_code, 404)
        self.client.force_login(self.recipient_user)
        self.assertEqual(self.reply(
            invitation, "yes", revision=event.rsvp_revision, key=self.first, csrf=False,
        ).status_code, 403)
        self.assertEqual(self.reply(
            invitation, "yes", revision=event.rsvp_revision, key="2027-11-29",
        ).status_code, 404)
        first = self.reply(invitation, "yes", revision=event.rsvp_revision, key=self.first)
        self.assertEqual(first.status_code, 200, first.content)
        token = first.json()["responded_at"]
        replay = self.reply(invitation, "yes", revision=event.rsvp_revision, key=self.first)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["responded_at"], token)
        self.assertEqual(self.reply(
            invitation, "no", revision=event.rsvp_revision, key=self.first,
        ).status_code, 409)
        update = self.reply(
            invitation, "no", revision=event.rsvp_revision, key=self.first, responded_at=token,
        )
        self.assertEqual(update.status_code, 200, update.content)
        self.assertEqual(CalendarOccurrenceRSVPHistory.objects.filter(invitation=invitation).count(), 2)

    def test_cancelled_occurrence_retains_answer_but_stops_new_replies_and_rule_cannot_erase_it(self):
        event = self.create_series()
        invitation = self.invite(event)
        self.client.force_login(self.recipient_user)
        response = self.reply(invitation, "maybe", revision=event.rsvp_revision, key=self.second)
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["responded_at"]
        self.client.force_login(self.owner)
        rule = write(self.client, "put", f"{self.base}{event.pk}/recurrence/", {
            "recurrence": {"frequency": "daily", "count": 1},
        })
        self.assertEqual(rule.status_code, 409, rule.content)
        cancelled = self.change_occurrence(event, self.second, "cancel/")
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.client.force_login(self.recipient_user)
        own = self.own(invitation, self.second)
        self.assertEqual(own["event"]["status"], "cancelled")
        self.assertFalse(own["can_respond"])
        self.assertEqual(own["response"], "maybe")
        self.assertEqual(self.reply(
            invitation, "no", revision=event.rsvp_revision, key=self.second, responded_at=token,
        ).status_code, 409)
        self.assertEqual(CalendarOccurrenceRSVPHistory.objects.filter(invitation=invitation).count(), 1)

    def test_series_group_snapshot_is_not_materialized_per_occurrence(self):
        event = self.create_series(mode="informational")
        department = Department.objects.create(organization=self.organization, name="Series invitees")
        DepartmentMembership.objects.create(
            organization=self.organization, department=department, organization_membership=self.recipient,
        )
        response = self.send(event, departments=[{"id": department.pk, "include_descendants": False}])
        self.assertEqual(response["created"], 1)
        invitation = event.invitations.get()
        DepartmentMembership.objects.create(
            organization=self.organization, department=department, organization_membership=self.other,
        )
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(self.own_url(invitation, self.first)).status_code, 404)
        self.assertEqual(self.client.get(self.inbox).json()["items"], [])
        self.client.force_login(self.recipient_user)
        self.assertFalse(self.own(invitation, self.first)["can_respond"])
        self.assertEqual(self.reply(
            invitation, "yes", revision=event.rsvp_revision, key=self.first,
        ).status_code, 409)
        self.assertEqual(event.invitations.count(), 1)
        self.assertFalse(CalendarOccurrenceRSVP.objects.filter(event=event).exists())
        self.client.force_login(self.owner)
        refreshed = write(self.client, "post", f"{self.base}{event.pk}/invitations/refresh/")
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        self.assertEqual(refreshed.json()["created"], 1)
        self.assertEqual(event.invitations.count(), 2)
