from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department, DepartmentMembership
from apps.notifications.models import Notification, NotificationEvent
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.onboarding import issue_invitation, revoke_invitation
from apps.organizations.services import create_organization
from apps.projects.models import Project, StaffingRequest
from apps.tasks.models import Task
from apps.tasks.services import set_assignee
from tests.test_department_memberships import write
from tests.test_projects import grant


class NotificationIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="notice-integration-owner")
        cls.worker = users.create_user(username="notice-integration-worker")
        cls.allocator = users.create_user(username="notice-integration-allocator")
        cls.reviewer = users.create_user(username="notice-integration-reviewer")
        cls.lead = users.create_user(username="notice-integration-lead")
        cls.bystander = users.create_user(username="notice-integration-bystander")
        cls.prejoin = users.create_user(username="notice-integration-prejoin")
        cls.org = create_organization(user=cls.owner, name="Notification integration workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)

        def member(user):
            return OrganizationMembership.objects.create(organization=cls.org, user=user)

        cls.worker_member = member(cls.worker)
        cls.allocator_member = member(cls.allocator)
        cls.reviewer_member = member(cls.reviewer)
        cls.lead_member = member(cls.lead)
        cls.bystander_member = member(cls.bystander)
        cls.department = Department.objects.create(organization=cls.org, name="Notification department")
        cls.worker_department = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.department, organization_membership=cls.worker_member,
        )
        DepartmentMembership.objects.create(
            organization=cls.org, department=cls.department, organization_membership=cls.bystander_member,
        )
        cls.project = Project.objects.create(
            organization=cls.org, name="Private staffing project", created_by=cls.owner,
            lead_membership=cls.lead_member, status=Project.Status.ACTIVE,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.base = f"/api/v1/organizations/{self.org.pk}/"
        self.inbox = "/api/v1/notifications/"

    def send(self, method, path, payload=None, *, status=200):
        response = write(self.client, method, self.base + path, payload)
        self.assertEqual(response.status_code, status, response.content)
        return response

    def notifications(self, kind, target):
        return Notification.objects.filter(
            event__organization=self.org, event__kind=kind, event__object_id=target.pk,
        )

    def assert_recipients(self, kind, target, *users):
        self.assertCountEqual(
            self.notifications(kind, target).values_list("recipient_id", flat=True),
            [user.pk for user in users],
        )

    def task(self, **fields):
        return Task.objects.create(
            organization=self.org, created_by=self.owner_member, name="Notification integration task",
            **fields,
        )

    def test_direct_assignment_collaboration_and_removal_notify_only_people_involved(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        task = self.task(parent=self.task())
        self.send("put", f"tasks/{task.pk}/assignee/", {"assignee_id": self.worker_member.pk})
        self.send("put", f"tasks/{task.pk}/assignee/", {"assignee_id": self.worker_member.pk})
        self.send("put", f"tasks/{task.pk}/collaborators/{self.reviewer_member.pk}/", status=204)
        self.assert_recipients("task.assigned", task, self.worker)
        self.assert_recipients("task.collaborator_added", task, self.reviewer)
        notification = self.notifications("task.assigned", task).get()
        self.client.force_login(self.worker)
        response = self.client.get(f"{self.inbox}{notification.pk}/link/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["url"], reverse("organization-task", args=[self.org.pk, task.pk]))
        self.client.force_login(self.owner)
        self.send("put", f"tasks/{task.pk}/assignee/", {"assignee_id": None})
        self.assert_recipients("task.unassigned", task, self.worker)
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(f"{self.inbox}{notification.pk}/link/").status_code, 404)

    def test_assignment_notification_rolls_back_with_real_task_service(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        task = self.task()
        with self.assertRaisesRegex(RuntimeError, "abort assignment"):
            with transaction.atomic():
                set_assignee(
                    user=self.owner, org_id=self.org.pk, task_id=task.pk,
                    assignee_id=self.worker_member.pk,
                )
                self.assert_recipients("task.assigned", task, self.worker)
                raise RuntimeError("abort assignment")
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)
        self.assertFalse(NotificationEvent.objects.filter(organization=self.org).exists())
        self.assertFalse(Notification.objects.filter(organization=self.org).exists())

    def test_department_distribution_targets_allocator_and_only_assignment_notifies_worker(self):
        grant(
            self.org, self.allocator_member, "tasks.distribution.view", "tasks.distribute",
            department=self.department,
        )
        task = self.task(department=self.department)
        self.send("post", f"tasks/{task.pk}/distribution/", {}, status=204)
        self.assert_recipients("task.distribution_requested", task, self.allocator)
        notification = self.notifications("task.distribution_requested", task).get()
        self.client.force_login(self.allocator)
        self.assertEqual(self.client.get(f"{self.inbox}{notification.pk}/link/").status_code, 200)
        self.send(
            "put", f"tasks/{task.pk}/distribution/", {"assignee_id": self.worker_member.pk},
            status=204,
        )
        self.assert_recipients("task.assigned", task, self.worker, self.owner)
        self.assertEqual(self.client.get(f"{self.inbox}{notification.pk}/link/").status_code, 404)

    def test_review_request_and_return_notify_current_reviewer_and_assignee(self):
        grant(
            self.org, self.reviewer_member, "tasks.review.approve", "tasks.review.return",
            department=self.department,
        )
        grant(self.org, self.worker_member, "tasks.review.submit")
        task = self.task(department=self.department, assignee=self.worker_member, status=Task.Status.IN_PROGRESS)
        self.send("put", f"tasks/{task.pk}/review/policy/", {"policy": "department", "reviewer_id": None}, status=204)
        self.client.force_login(self.worker)
        self.send("post", f"tasks/{task.pk}/review/submissions/", {"comment": "Ready", "links": []}, status=204)
        self.assert_recipients("review.requested", task, self.reviewer, self.owner)
        self.client.force_login(self.reviewer)
        stage = self.client.get(f"{self.base}tasks/{task.pk}/workflow/").json()["review"]["current_stage_id"]
        self.send("post", f"tasks/{task.pk}/review/decisions/", {
            "stage_id": stage, "outcome": "returned", "comment": "Fix the result",
        }, status=204)
        self.assert_recipients("review.returned", task, self.worker)
        self.assertFalse(self.notifications("review.returned", task).filter(recipient=self.bystander).exists())

    def test_organization_and_department_invites_respect_personal_and_tenant_access(self):
        invitation, _ = issue_invitation(actor=self.owner, org_id=self.org.pk, username=self.prejoin.username)
        self.assert_recipients("organization.invited", invitation, self.prejoin)
        notification = self.notifications("organization.invited", invitation).get()
        self.client.force_login(self.prejoin)
        self.assertFalse(self.org.memberships.filter(user=self.prejoin).exists())
        inbox = self.client.get(self.inbox).json()
        self.assertEqual([row["id"] for row in inbox["items"]], [notification.pk])
        link = self.client.get(f"{self.inbox}{notification.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertEqual(link.json()["url"], reverse("organization-invitations"))
        self.assertEqual(self.client.get(f"{self.base}").status_code, 404)
        revoke_invitation(actor=self.owner, org_id=self.org.pk, invitation_id=invitation.pk)
        self.assertEqual(self.client.get(f"{self.inbox}{notification.pk}/link/").status_code, 404)

        self.client.force_login(self.owner)
        invited = self.send("post", f"departments/{self.department.pk}/invitations/", {
            "organization_membership_id": self.lead_member.pk,
        }, status=201)
        department_invitation = self.department.invitations.get(pk=invited.json()["id"])
        self.assert_recipients("department.invited", department_invitation, self.lead)

    def test_staffing_request_and_project_invitation_reach_allocator_recipient_and_initiator(self):
        grant(
            self.org, self.allocator_member, "departments.staffing.allocate", department=self.department,
        )
        Organization.objects.filter(pk=self.org.pk).update(project_assignment_mode="invitation")
        draft = self.send("post", f"projects/{self.project.pk}/staffing-requests/", {
            "department_id": self.department.pk, "requested_count": 1, "description": "Help with planning",
        }, status=201)
        request = StaffingRequest.objects.get(pk=draft.json()["id"])
        self.assert_recipients("staffing.requested", request)
        self.send("post", f"staffing-requests/{request.pk}/submit/")
        self.assert_recipients("staffing.requested", request, self.allocator)
        self.client.force_login(self.allocator)
        self.send("post", f"staffing-requests/{request.pk}/allocate/", {
            "department_membership_ids": [self.worker_department.pk],
        })
        invitation = request.allocations.get()
        self.assert_recipients("project.invited", invitation, self.worker)
        notice = self.notifications("project.invited", invitation).get()
        self.client.force_login(self.worker)
        self.assertEqual(
            self.client.get(f"{self.inbox}{notice.pk}/link/").json()["url"], reverse("project-invitations"),
        )
        response_url = f"/api/v1/organizations/project-invitations/{invitation.pk}/respond/"
        reply = write(self.client, "post", response_url, {"decision": "accept"})
        self.assertEqual(reply.status_code, 200, reply.content)
        self.assert_recipients("project.invitation_accepted", request, self.allocator)
        self.assert_recipients("staffing.fulfilled", request, self.owner, self.lead)

    def test_calendar_deduplicates_group_invites_and_links_to_changed_occurrence_without_answering(self):
        created = self.send("post", "events/", {
            "title": "Repeated notification meeting", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
            "rsvp_mode": "required", "recurrence": {"frequency": "daily"},
        }, status=201)
        event = CalendarEvent.objects.get(pk=created.json()["id"])
        recipients = {
            "member_ids": [self.worker_member.pk],
            "departments": [{"id": self.department.pk, "include_descendants": False}],
        }
        self.send("post", f"events/{event.pk}/invitations/", recipients, status=201)
        self.send("post", f"events/{event.pk}/invitations/", recipients, status=201)
        self.assert_recipients("calendar.invited", event, self.worker, self.bystander)
        invitation = event.invitations.get(recipient=self.worker_member)
        self.client.force_login(self.worker)
        answer = write(self.client, "post", f"/api/v1/organizations/calendar/invitations/{invitation.pk}/respond/", {
            "response": "yes", "expected_revision": event.rsvp_revision, "expected_response_at": None,
        })
        self.assertEqual(answer.status_code, 200, answer.content)
        self.client.force_login(self.owner)
        key = "2027-11-16"
        self.send("patch", f"events/{event.pk}/occurrences/{key}/", {
            "expected_version": 0, "starts_at": "2027-11-16T18:00:00+00:00",
            "ends_at": "2027-11-16T19:00:00+00:00",
        })
        self.assert_recipients("calendar.occurrence_rescheduled", event, self.worker, self.bystander)
        changed = self.notifications("calendar.occurrence_rescheduled", event).get(recipient=self.worker)
        self.assertEqual(changed.event.data, {"occurrence_key": key})
        self.client.force_login(self.worker)
        link = self.client.get(f"{self.inbox}{changed.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertTrue(link.json()["url"].endswith(f"/{invitation.pk}/?occurrence={key}"))
        self.assertEqual(write(self.client, "post", f"{self.inbox}{changed.pk}/read/").status_code, 200)
        invitation.refresh_from_db()
        self.assertEqual(invitation.response, "yes")
        self.assertFalse(event.occurrence_responses.exists())
        self.client.force_login(self.owner)
        self.send("post", f"events/{event.pk}/occurrences/{key}/cancel/", {"expected_version": 1})
        self.assert_recipients("calendar.occurrence_cancelled", event, self.worker, self.bystander)
        self.send("post", f"events/{event.pk}/cancel/")
        self.assert_recipients("calendar.cancelled", event, self.worker, self.bystander)
