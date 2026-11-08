from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.calendar_app.invitation_models import CalendarAudience, CalendarInvitation, CalendarRSVPHistory
from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from tests.test_department_memberships import write
from tests.test_projects import grant


def event_record(organization, creator, **fields):
    return CalendarEvent.objects.create(
        organization=organization, created_by=creator, title="Invitation meeting",
        event_type=CalendarEvent.Type.MEETING, timezone="Asia/Almaty",
        starts_at=datetime.fromisoformat("2027-11-09T09:00:00+05:00"),
        ends_at=datetime.fromisoformat("2027-11-09T10:00:00+05:00"),
        **fields,
    )


class CalendarInvitationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="invitations-calendar-owner")
        cls.foreign_owner = users.create_superuser(username="invitations-calendar-foreign")
        cls.org = create_organization(user=cls.owner, name="Calendar invitations")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign calendar invitations")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)

        def member(username, *, active=True):
            user = users.create_user(username=f"cal-audience-{username}")
            return OrganizationMembership.objects.create(organization=cls.org, user=user, is_active=active)

        cls.overlap = member("overlap")
        cls.child_member = member("child")
        cls.project_member = member("project")
        cls.recipient = member("recipient")
        cls.later = member("later")
        cls.manager = member("manager")
        cls.inactive = member("inactive", active=False)
        cls.department = Department.objects.create(organization=cls.org, name="Calendar audience")
        cls.child_department = Department.objects.create(
            organization=cls.org, parent=cls.department, name="Calendar child audience",
        )
        cls.other_department = Department.objects.create(
            organization=cls.org, name="Calendar other audience",
        )
        cls.foreign_department = Department.objects.create(
            organization=cls.foreign, name="Foreign department audience",
        )
        cls.project = Project.objects.create(
            organization=cls.org, name="Private calendar project", created_by=cls.owner,
            responsible_department=cls.department,
        )
        cls.foreign_project = Project.objects.create(
            organization=cls.foreign, name="Foreign calendar project", created_by=cls.foreign_owner,
        )
        for department, membership in (
            (cls.department, cls.overlap),
            (cls.child_department, cls.overlap),
            (cls.child_department, cls.child_member),
        ):
            DepartmentMembership.objects.create(
                organization=cls.org, department=department, organization_membership=membership,
            )
        for membership in (cls.overlap, cls.project_member):
            ProjectMembership.objects.create(
                organization=cls.org, project=cls.project, organization_membership=membership,
            )
        cls.event = event_record(cls.org, cls.owner_member)
        cls.foreign_event = event_record(cls.foreign, cls.foreign_member)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.base = f"/api/v1/organizations/{self.org.pk}/"

    def event_url(self, action="", *, event=None):
        return f"{self.base}events/{(event or self.event).pk}/{action}"

    def audience_url(self, action="", *, event=None):
        return self.event_url(f"invitations/{action}", event=event)

    def send(self, *, event=None, **audience):
        return write(self.client, "post", self.audience_url(event=event), audience)

    def active_recipients(self, *, event=None):
        return set((event or self.event).invitations.filter(revoked_at__isnull=True)
                   .values_list("recipient_id", flat=True))

    def own_url(self, invitation=None, action=""):
        base = "/api/v1/organizations/calendar/invitations/"
        return f"{base}{invitation.pk}/{action}" if invitation is not None else base

    def reply(self, invitation, response, *, revision=1, responded_at=None, csrf=True, **extra):
        return write(
            self.client, "post", self.own_url(invitation, "respond/"),
            {
                "response": response, "expected_revision": revision,
                "expected_response_at": responded_at, **extra,
            },
            csrf=csrf,
        )

    def invite_one(self, event=None, recipient=None):
        recipient = recipient or self.recipient
        event = event or self.event
        response = self.send(event=event, member_ids=[recipient.pk])
        self.assertEqual(response.status_code, 201, response.content)
        return event.invitations.get(recipient=recipient)

    def test_event_without_invitation_keeps_calendar_default_and_identity(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.event_url())
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["id"], self.event.pk)
        self.assertEqual(result["rsvp_mode"], "informational")
        self.assertEqual(result["rsvp_revision"], 1)
        self.assertIsNone(result["invitation_id"])
        self.assertFalse(CalendarInvitation.objects.filter(event=self.event).exists())

    def test_database_guards_active_uniqueness_and_foreign_recipient(self):
        base = {"event": self.event, "organization": self.org, "invited_by": self.owner_member}
        CalendarInvitation.objects.create(**base, recipient=self.recipient)
        for recipient in (self.recipient, self.foreign_member):
            with self.subTest(recipient=recipient.pk), self.assertRaises(IntegrityError), transaction.atomic():
                CalendarInvitation.objects.create(**base, recipient=recipient)
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CalendarAudience.objects.create(
                organization=self.org, event=self.event, created_by=self.owner_member,
                kind=CalendarAudience.Kind.DEPARTMENT, department=self.foreign_department,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(self.event.invitations.count(), 1)

    def test_direct_department_and_project_snapshots_deduplicate_with_sources(self):
        self.client.force_login(self.owner)
        direct = self.send(departments=[{"id": self.department.pk, "include_descendants": False}])
        self.assertEqual(direct.status_code, 201, direct.content)
        self.assertEqual(direct.json()["created"], 1)
        self.assertEqual(self.active_recipients(), {self.overlap.pk})
        audiences = {
            "member_ids": [self.overlap.pk, self.recipient.pk],
            "departments": [{"id": self.department.pk, "include_descendants": True}],
            "project_ids": [self.project.pk],
        }
        preview = write(self.client, "post", self.audience_url("preview/"), audiences)
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json(), {"total": 4, "new": 3, "already_invited": 1, "revoked": 0})
        self.assertEqual(self.active_recipients(), {self.overlap.pk})
        combined = self.send(**audiences)
        self.assertEqual(combined.status_code, 201, combined.content)
        self.assertEqual(combined.json(), {"total": 4, "created": 3, "already_invited": 1, "revoked": 0})
        self.assertEqual(self.active_recipients(), {
            self.overlap.pk, self.recipient.pk, self.child_member.pk, self.project_member.pk,
        })
        overlap = self.event.invitations.get(recipient=self.overlap)
        self.assertEqual(overlap.sources.count(), 4)
        self.assertFalse(ProjectMembership.objects.filter(
            project=self.project, organization_membership=self.recipient,
        ).exists())
        repeated = self.send(**audiences)
        self.assertEqual(repeated.status_code, 201, repeated.content)
        self.assertEqual(repeated.json()["created"], 0)
        self.assertEqual(self.event.invitations.count(), 4)
        self.assertEqual(overlap.sources.count(), 4)

    def test_whole_organization_excludes_inactive_and_refresh_adds_only_new_matches(self):
        self.client.force_login(self.owner)
        all_members = self.send(organization=True)
        self.assertEqual(all_members.status_code, 201, all_members.content)
        self.assertEqual(
            self.active_recipients(),
            set(self.org.memberships.filter(is_active=True, user__is_active=True).values_list("id", flat=True)),
        )
        self.assertNotIn(self.inactive.pk, self.active_recipients())
        self.assertNotIn(self.foreign_member.pk, self.active_recipients())

        scoped_event = event_record(self.org, self.owner_member)
        first = self.send(event=scoped_event, project_ids=[self.project.pk])
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(self.active_recipients(event=scoped_event), {self.overlap.pk, self.project_member.pk})
        ProjectMembership.objects.create(
            organization=self.org, project=self.project, organization_membership=self.later,
        )
        self.assertNotIn(self.later.pk, self.active_recipients(event=scoped_event))
        refreshed = write(self.client, "post", self.audience_url("refresh/", event=scoped_event))
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        self.assertEqual(refreshed.json()["created"], 1)
        self.assertEqual(
            self.active_recipients(event=scoped_event), {self.overlap.pk, self.project_member.pk, self.later.pk},
        )
        again = write(self.client, "post", self.audience_url("refresh/", event=scoped_event))
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["created"], 0)

    def test_audience_requests_are_tenant_scoped_csrf_protected_and_atomic(self):
        explicit = {"member_ids": [self.recipient.pk]}
        self.assertEqual(
            write(self.client, "post", self.audience_url("preview/"), explicit).status_code, 401,
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            write(self.client, "post", self.audience_url(), explicit, csrf=False).status_code, 403,
        )
        for foreign in (
            {"member_ids": [self.recipient.pk, self.foreign_member.pk]},
            {"departments": [{"id": self.foreign_department.pk, "include_descendants": True}]},
            {"project_ids": [self.foreign_project.pk]},
        ):
            with self.subTest(foreign=foreign):
                response = self.send(**(explicit | foreign))
                self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(
            write(self.client, "post", self.audience_url(event=self.foreign_event), explicit).status_code, 404,
        )
        self.assertFalse(self.event.invitations.exists())
        self.assertFalse(self.event.audiences.exists())
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.audience_url("options/")).status_code, 404)

    def test_calendar_reader_cannot_enumerate_or_manage_audience(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.send(member_ids=[self.recipient.pk]).status_code, 201)
        grant(self.org, self.manager, "calendar.read")
        self.client.force_login(self.manager.user)
        self.assertEqual(self.client.get(self.event_url()).status_code, 200)
        for action in ("", "options/"):
            with self.subTest(action=action):
                self.assertEqual(self.client.get(self.audience_url(action)).status_code, 403)
        self.assertEqual(self.send(member_ids=[self.later.pk]).status_code, 403)
        self.assertEqual(self.active_recipients(), {self.recipient.pk})


    def test_scoped_inviter_cannot_preview_protected_groups_or_enumerate_members(self):
        event = event_record(self.org, self.owner_member, department=self.department)
        grant(self.org, self.manager, "calendar.read", "calendar.invite", department=self.department)
        self.client.force_login(self.manager.user)
        response = self.client.get(self.audience_url("options/", event=event), {"q": "cal-audience"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["members"], [])
        self.assertEqual(data["member_total"], 0)
        self.assertFalse(data["can_invite_organization"])
        self.assertEqual([row["id"] for row in data["departments"]], [self.department.pk])
        self.assertFalse(data["departments"][0]["can_include_descendants"])
        for denied in (
            {"organization": True},
            {"member_ids": [self.recipient.pk]},
            {"departments": [{"id": self.department.pk, "include_descendants": True}]},
            {"departments": [{"id": self.other_department.pk, "include_descendants": False}]},
        ):
            with self.subTest(audience=denied):
                response = write(
                    self.client, "post", self.audience_url("preview/", event=event), denied,
                )
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(self.send(event=event, **denied).status_code, 403)
        self.assertFalse(event.audiences.exists())
        self.assertFalse(event.invitations.exists())


    def test_invitation_only_access_redacts_scope_and_prevents_idor(self):
        event = event_record(
            self.org, self.owner_member, project=self.project, department=self.department,
            description="Confidential project agenda", location="Room 8",
        )
        self.client.force_login(self.owner)
        invitation = self.invite_one(event=event)
        private_page = reverse("organization-calendar-invitation", args=[self.org.pk, invitation.pk])
        self.client.force_login(self.later.user)
        self.assertEqual(self.client.get(self.event_url(event=event)).status_code, 404)
        self.assertEqual(self.client.get(self.own_url(invitation)).status_code, 404)
        self.assertEqual(self.client.get(private_page).status_code, 404)

        self.client.force_login(self.recipient.user)
        detail = self.client.get(self.event_url(event=event))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["invitation_id"], invitation.pk)
        self.assertEqual(detail.json()["location"], "Room 8")
        self.assertEqual(detail.json()["description"], "")
        self.assertIsNone(detail.json()["project_id"])
        self.assertIsNone(detail.json()["department_id"])
        self.assertFalse(detail.json()["can_edit"])
        self.assertFalse(detail.json()["can_manage_invitations"])
        self.assertEqual(self.client.get(private_page).status_code, 200)
        self.assertIn(
            self.client.get(self.audience_url(event=event)).status_code, {403, 404},
        )
        own = self.client.get(self.own_url(invitation))
        self.assertEqual(own.status_code, 200, own.content)
        self.assertNotIn("Confidential project agenda", own.content.decode())
        self.assertNotIn("Private calendar project", own.content.decode())
        self.assertEqual(own.json()["event"]["id"], event.pk)
        self.assertNotIn("project_id", own.json()["event"])
        self.assertNotIn("department_id", own.json()["event"])

        url = f"{self.base}calendar/"
        bounds = {"start": "2027-11-09", "end": "2027-11-10", "timezone": "Asia/Almaty"}
        visible = self.client.get(url, bounds)
        self.assertEqual(visible.status_code, 200, visible.content)
        self.assertEqual([row["id"] for row in visible.json()["items"]], [event.pk])
        self.assertIsNone(visible.json()["items"][0]["project_id"])
        filtered = self.client.get(url, bounds | {"project_id": self.project.pk})
        self.assertEqual(filtered.status_code, 200, filtered.content)
        self.assertEqual(filtered.json()["items"], [])

    def test_three_rsvp_modes_and_idempotent_own_answer_history(self):
        self.client.force_login(self.owner)
        info = self.invite_one()
        optional_event = event_record(self.org, self.owner_member, rsvp_mode="optional")
        required_event = event_record(self.org, self.owner_member, rsvp_mode="required")
        optional = self.invite_one(event=optional_event)
        required = self.invite_one(event=required_event)
        self.client.force_login(self.recipient.user)

        self.assertEqual(self.reply(info, "yes").status_code, 409)
        for invitation, mode in ((info, "informational"), (optional, "optional"), (required, "required")):
            with self.subTest(mode=mode):
                response = self.client.get(self.own_url(invitation))
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["rsvp_mode"], mode)
                self.assertIsNone(response.json()["response"])
                self.assertEqual(response.json()["can_respond"], mode != "informational")

        self.assertEqual(self.reply(optional, "maybe").status_code, 200)
        first = self.reply(required, "no")
        self.assertEqual(first.status_code, 200, first.content)
        token = first.json()["responded_at"]
        repeated = self.reply(required, "no")
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertEqual(repeated.json()["responded_at"], token)
        self.assertEqual(self.reply(required, "yes").status_code, 409)
        for answer in ("yes", "maybe"):
            with self.subTest(answer=answer):
                response = self.reply(required, answer, responded_at=token)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["response"], answer)
                token = response.json()["responded_at"]
        self.assertEqual(
            list(required.response_history.order_by("pk").values_list("response", flat=True)),
            ["no", "yes", "maybe"],
        )
        self.assertFalse(info.response_history.exists())

        self.client.force_login(self.owner)
        stats = self.client.get(self.audience_url("stats/", event=required_event))
        self.assertEqual(stats.status_code, 200, stats.content)
        self.assertEqual({key: stats.json()[key] for key in ("invited", "yes", "no", "maybe", "pending")}, {
            "invited": 1, "yes": 0, "no": 0, "maybe": 1, "pending": 0,
        })

    def test_schedule_revision_requires_reconfirmation_and_retains_history(self):
        event = event_record(self.org, self.owner_member, rsvp_mode="optional")
        self.client.force_login(self.owner)
        invitation = self.invite_one(event=event)
        self.client.force_login(self.recipient.user)
        self.assertEqual(self.reply(invitation, "yes").status_code, 200)
        self.client.force_login(self.owner)
        update = write(self.client, "patch", self.event_url(event=event), {
            "title": "Retitled meeting", "rsvp_mode": "required",
        })
        self.assertEqual(update.status_code, 200, update.content)
        self.assertEqual(update.json()["rsvp_revision"], 1)
        moved = write(self.client, "patch", self.event_url(event=event), {
            "starts_at": "2027-11-09T10:00:00+05:00", "ends_at": "2027-11-09T11:00:00+05:00",
        })
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual(moved.json()["rsvp_revision"], 2)
        stats = self.client.get(self.audience_url("stats/", event=event)).json()
        self.assertEqual((stats["yes"], stats["pending"], stats["needs_reconfirmation"]), (0, 1, 1))

        self.client.force_login(self.recipient.user)
        own = self.client.get(self.own_url(invitation)).json()
        self.assertIsNone(own["response"])
        self.assertTrue(own["needs_reconfirmation"])
        token = own["responded_at"]
        self.assertEqual(self.reply(invitation, "no", revision=1, responded_at=token).status_code, 409)
        self.assertEqual(self.reply(invitation, "no", revision=2, responded_at=token).status_code, 200)
        self.assertEqual(
            list(invitation.response_history.order_by("pk").values_list("response", "response_revision")),
            [("yes", 1), ("no", 2)],
        )
        self.client.force_login(self.owner)
        history = self.client.get(self.audience_url(f"{invitation.pk}/history/", event=event))
        self.assertEqual(history.status_code, 200, history.content)
        self.assertCountEqual([entry["response"] for entry in history.json()["items"]], ["yes", "no"])

    def test_rsvp_cannot_be_spoofed_and_closed_events_preserve_history(self):
        event = event_record(self.org, self.owner_member, rsvp_mode="required")
        self.client.force_login(self.owner)
        invitation = self.invite_one(event=event)
        self.assertEqual(self.reply(invitation, "yes").status_code, 404)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.reply(invitation, "yes").status_code, 404)
        self.client.force_login(self.recipient.user)
        self.assertEqual(self.reply(invitation, "yes", csrf=False).status_code, 403)
        self.assertEqual(self.reply(invitation, "yes", user_id=self.later.user_id).status_code, 422)
        self.assertEqual(self.reply(invitation, "no").status_code, 200)

        for action, status in (("cancel/", "cancelled"), ("archive/", "archived")):
            with self.subTest(status=status):
                self.client.force_login(self.owner)
                changed = write(self.client, "post", self.event_url(action, event=event))
                self.assertEqual(changed.status_code, 200, changed.content)
                self.client.force_login(self.recipient.user)
                own = self.client.get(self.own_url(invitation))
                self.assertEqual(own.status_code, 200, own.content)
                self.assertEqual(own.json()["event"]["status"], status)
                self.assertFalse(own.json()["can_respond"])
                self.assertEqual(self.reply(invitation, "yes").status_code, 409)
        self.assertEqual(list(invitation.response_history.values_list("response", flat=True)), ["no"])

    def test_departure_revokes_invited_access_and_reactivation_preserves_old_history(self):
        event = event_record(self.org, self.owner_member, rsvp_mode="required")
        self.client.force_login(self.owner)
        invitation = self.invite_one(event=event)
        self.client.force_login(self.recipient.user)
        self.assertEqual(self.reply(invitation, "no").status_code, 200)

        OrganizationMembership.objects.filter(pk=self.recipient.pk).update(is_active=False)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        self.assertEqual(self.client.get(self.event_url(event=event)).status_code, 404)
        self.assertEqual(self.client.get(self.own_url(invitation)).status_code, 404)
        OrganizationMembership.objects.filter(pk=self.recipient.pk).update(is_active=True)
        self.assertEqual(self.client.get(self.own_url(invitation)).status_code, 404)
        self.assertEqual(self.client.get(self.event_url(event=event)).status_code, 404)

        self.client.force_login(self.owner)
        history = self.client.get(self.audience_url(f"{invitation.pk}/history/", event=event))
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual([row["response"] for row in history.json()["items"]], ["no"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            CalendarRSVPHistory.objects.filter(invitation=invitation).update(response="yes")
        retried = self.send(event=event, member_ids=[self.recipient.pk])
        self.assertEqual(retried.status_code, 201, retried.content)
        self.assertEqual(retried.json()["created"], 0)
        reissued = self.send(event=event, member_ids=[self.recipient.pk], reinvite_revoked=True)
        self.assertEqual(reissued.status_code, 201, reissued.content)
        self.assertEqual(reissued.json()["created"], 1)
        active = event.invitations.get(recipient=self.recipient, revoked_at__isnull=True)
        self.assertNotEqual(active.pk, invitation.pk)
        self.assertIsNone(active.response)

    def test_scoped_manager_cannot_revoke_audience_outside_managed_scope(self):
        event = event_record(self.org, self.owner_member, department=self.department)
        self.client.force_login(self.owner)
        invitation = self.invite_one(event=event)
        revoke = self.audience_url(f"{invitation.pk}/revoke/", event=event)
        grant(
            self.org, self.manager, "calendar.read", "calendar.invite", "calendar.invite.manage",
            department=self.department,
        )
        self.client.force_login(self.manager.user)
        self.assertEqual(write(self.client, "post", revoke).status_code, 403)
        self.assertEqual(self.active_recipients(event=event), {self.recipient.pk})

        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "post", revoke, csrf=False).status_code, 403)
        self.assertEqual(write(self.client, "post", revoke).status_code, 204)
        self.assertEqual(self.active_recipients(event=event), set())
        self.client.force_login(self.recipient.user)
        self.assertEqual(self.client.get(self.event_url(event=event)).status_code, 404)
        self.assertEqual(self.client.get(self.own_url(invitation)).status_code, 404)


class ConcurrentCalendarInvitationTests(TransactionTestCase):
    def parallel_requests(self, user, url, payloads):
        start = Barrier(len(payloads), timeout=8)

        def send(payload):
            worker_connection = connections["default"]
            try:
                client = Client(enforce_csrf_checks=True)
                client.force_login(user)
                with worker_connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                start.wait()
                response = write(client, "post", url, payload)
                return response.status_code, response.json()
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            futures = [pool.submit(send, payload) for payload in payloads]
            return [future.result(timeout=16) for future in futures]

    def test_concurrent_identical_sends_create_one_invitation_and_one_source(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="calendar-concurrent-owner")
        user = users.create_user(username="calendar-concurrent-recipient")
        organization = create_organization(user=owner, name="Calendar concurrency")
        creator = organization.memberships.get(user=owner)
        recipient = OrganizationMembership.objects.create(organization=organization, user=user)
        event = event_record(organization, creator)
        url = f"/api/v1/organizations/{organization.pk}/events/{event.pk}/invitations/"
        results = self.parallel_requests(owner, url, [{"member_ids": [recipient.pk]}] * 2)
        for status, payload in results:
            self.assertEqual(status, 201, payload)
        self.assertEqual(sorted(payload["created"] for _, payload in results), [0, 1])
        self.assertEqual(event.audiences.count(), 1)
        self.assertEqual(event.invitations.count(), 1)
        self.assertEqual(event.invitations.get().sources.count(), 1)

    def test_concurrent_distinct_replies_allow_only_one_initial_response(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="calendar-reply-race-owner")
        user = users.create_user(username="calendar-reply-race-recipient")
        organization = create_organization(user=owner, name="Calendar RSVP concurrency")
        creator = organization.memberships.get(user=owner)
        recipient = OrganizationMembership.objects.create(organization=organization, user=user)
        event = event_record(organization, creator, rsvp_mode="required")
        invitation = CalendarInvitation.objects.create(
            organization=organization, event=event, recipient=recipient, invited_by=creator,
        )
        url = f"/api/v1/organizations/calendar/invitations/{invitation.pk}/respond/"
        results = self.parallel_requests(user, url, [
            {"response": answer, "expected_revision": 1, "expected_response_at": None}
            for answer in ("yes", "no")
        ])
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        invitation.refresh_from_db()
        self.assertEqual(invitation.response, winner["response"])
        self.assertEqual(list(invitation.response_history.values_list("response", flat=True)), [
            winner["response"],
        ])
