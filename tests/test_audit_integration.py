from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase

from apps.audit.models import AuditEvent
from apps.calendar_app.models import CalendarEvent
from apps.departments.models import Department, DepartmentMembership
from apps.notifications.models import NotificationEvent
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.onboarding import issue_invitation, respond_to_invitation
from apps.organizations.services import create_organization
from apps.projects.models import Project, StaffingRequest
from apps.tasks.models import Task
from apps.tasks.services import set_assignee
from tests.test_department_memberships import write
from tests.test_projects import grant


class AuditIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="audit-integration-owner")
        cls.worker = users.create_user(username="audit-integration-worker")
        cls.reader = users.create_user(username="audit-integration-reader")
        cls.prejoin = users.create_user(username="audit-integration-prejoin")
        cls.org = create_organization(user=cls.owner, name="Audit integration")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.worker_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.worker)
        cls.reader_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.reader)
        cls.department = Department.objects.create(organization=cls.org, name="Audit source")
        cls.destination = Department.objects.create(organization=cls.org, name="Audit destination")
        cls.department_member = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.department, organization_membership=cls.worker_member,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.base = f"/api/v1/organizations/{self.org.pk}/"
        self.audit = f"{self.base}audit/"

    def change(self, method, suffix, payload=None, *, status=200):
        result = write(self.client, method, f"{self.base}{suffix}", payload)
        self.assertEqual(result.status_code, status, result.content)
        return result

    def event(self, action, object_id):
        return AuditEvent.objects.get(organization=self.org, action=action, object_id=object_id)

    def test_organization_creation_and_prejoin_invitation_are_attributed_without_token(self):
        created = self.event("organization.created", self.org.pk)
        self.assertEqual(created.actor_id, self.owner.pk)
        invitation, token = issue_invitation(actor=self.owner, org_id=self.org.pk, username=self.prejoin.username)
        issued = AuditEvent.objects.get(
            organization=self.org, object_type="organization_invitation", object_id=invitation.pk,
        )
        self.assertEqual(issued.actor_id, self.owner.pk)
        self.assertNotIn(token, str((issued.changes, issued.metadata)))
        self.assertNotIn(invitation.token_digest, str((issued.changes, issued.metadata)))
        respond_to_invitation(actor=self.prejoin, invitation_id=invitation.pk, accept=True)
        accepted = self.event("organization_invitation.accepted", invitation.pk)
        self.assertEqual(accepted.actor_id, self.prejoin.pk)
        self.assertTrue(self.org.memberships.get(user=self.prejoin).is_active)

    def test_department_move_records_both_old_and_new_parent_and_rejects_cycle_without_audit(self):
        response = self.change("post", "departments/", {"name": "Audited child", "parent_id": self.department.pk}, status=201)
        child_id = response.json()["id"]
        self.assertEqual(self.event("department.created", child_id).actor_id, self.owner.pk)
        self.change("patch", f"departments/{child_id}/", {"parent_id": self.destination.pk})
        move = self.event("department.moved", child_id)
        self.assertEqual(move.changes["parent_id"], {"old": self.department.pk, "new": self.destination.pk})
        self.assertEqual(move.department_id, child_id)
        self.assertCountEqual(move.metadata["affected_department_ids"], [
            self.department.pk, self.destination.pk,
        ])
        before = AuditEvent.objects.filter(organization=self.org).count()
        self.change("patch", f"departments/{self.destination.pk}/", {"parent_id": child_id}, status=409)
        self.assertEqual(AuditEvent.objects.filter(organization=self.org).count(), before)

    def test_role_diff_assignment_and_revocation_preserve_permissions_and_actor(self):
        permissions = [{"code": "tasks.view", "include_descendants": False}]
        created = self.change("post", "roles/", {"name": "Audited role", "permissions": permissions}, status=201)
        role_id = created.json()["id"]
        updated = [{"code": "tasks.view", "include_descendants": True}, {
            "code": "tasks.update", "include_descendants": False,
        }]
        self.change("patch", f"roles/{role_id}/", {"permissions": updated})
        diff = self.event("role.updated", role_id)
        self.assertEqual(diff.changes["permissions"]["old"], permissions)
        self.assertCountEqual(diff.changes["permissions"]["new"], updated)
        assigned = self.change("post", "role-assignments/", {
            "role_id": role_id, "organization_membership_id": self.worker_member.pk,
            "department_membership_id": self.department_member.pk,
        }, status=201)
        assignment_id = assigned.json()["id"]
        self.change("delete", f"role-assignments/{assignment_id}/", status=204)
        for action in ("role_assignment.assigned", "role_assignment.revoked"):
            event = self.event(action, assignment_id)
            self.assertEqual(event.actor_id, self.owner.pk)
            self.assertEqual(event.department_id, self.department.pk)
            self.assertEqual(event.metadata["organization_membership_id"], self.worker_member.pk)
            self.assertEqual(event.metadata["role_id"], role_id)
        grant(self.org, self.reader_member, "audit.read_all", "audit.read_details")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(f"{self.audit}{diff.pk}/").status_code, 404)
        grant(self.org, self.reader_member, "roles.read")
        detail = self.client.get(f"{self.audit}{diff.pk}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["changes"]["permissions"], diff.changes["permissions"])

    def test_task_create_assignment_status_and_deadline_are_audited_with_redacted_description(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        created = self.change("post", "tasks/", {"name": "Audited task", "department_id": self.department.pk}, status=201)
        task_id = created.json()["id"]
        self.assertEqual(self.event("task.created", task_id).actor_id, self.owner.pk)
        self.change("put", f"tasks/{task_id}/assignee/", {"assignee_id": self.worker_member.pk})
        self.assertEqual(self.event("task.assigned", task_id).changes["assignee_id"], {
            "old": None, "new": self.worker_member.pk,
        })
        deadline = "2027-11-09T14:00:00+00:00"
        self.change("patch", f"tasks/{task_id}/", {"due_at": deadline, "description": "Private task body"})
        diff = self.event("task.updated", task_id)
        self.assertIsNone(diff.changes["due_at"]["old"])
        self.assertEqual(datetime.fromisoformat(diff.changes["due_at"]["new"]), datetime.fromisoformat(deadline))
        self.assertNotIn("Private task body", str(diff.changes))
        self.change("put", f"tasks/{task_id}/status/", {"status": "in_progress"})
        self.assertEqual(self.event("task.status_changed", task_id).changes["status"], {
            "old": "pending", "new": "in_progress",
        })

    def test_required_audit_write_failure_rolls_back_assignment_and_notification(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        task = Task.objects.create(organization=self.org, created_by=self.owner_member, name="Audit failure task")
        with patch("apps.audit.producer.record", side_effect=IntegrityError("audit failed")):
            with self.assertRaisesRegex(IntegrityError, "audit failed"):
                set_assignee(user=self.owner, org_id=self.org.pk, task_id=task.pk, assignee_id=self.worker_member.pk)
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)
        self.assertFalse(task.distributions.exists())
        self.assertFalse(NotificationEvent.objects.filter(organization=self.org, object_type="task", object_id=task.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(organization=self.org, object_type="task", object_id=task.pk).exists())

    def test_review_audit_records_stage_and_version_but_never_result_text_or_links(self):
        grant(self.org, self.worker_member, "tasks.review.submit")
        task = Task.objects.create(
            organization=self.org, created_by=self.owner_member, assignee=self.worker_member,
            department=self.department, name="Audited result", status=Task.Status.IN_PROGRESS,
        )
        for version, decision in ((1, "returned"), (2, "approved")):
            self.client.force_login(self.worker)
            self.change("post", f"tasks/{task.pk}/review/submissions/", {
                "comment": "Confidential result text", "links": ["https://example.invalid/private-result"],
            }, status=204)
            stage = task.review_submissions.order_by("-pk").first().stages.get()
            self.client.force_login(self.owner)
            self.change("post", f"tasks/{task.pk}/review/decisions/", {
                "stage_id": stage.pk, "outcome": decision, "comment": "Confidential decision note",
            }, status=204)
            row = self.event(f"task.review_{decision}", task.pk)
            self.assertEqual(row.metadata["submission_version"], version)
            self.assertEqual(row.metadata["stage_id"], stage.pk)
            self.assertEqual(row.metadata["stage_position"], 1)
        audit_rows = AuditEvent.objects.filter(organization=self.org, object_type="task", object_id=task.pk)
        self.assertEqual(audit_rows.filter(action="task.review_submitted").count(), 2)
        self.assertNotIn("Confidential", str(list(audit_rows.values("changes", "metadata"))))
        self.assertNotIn("private-result", str(list(audit_rows.values("changes", "metadata"))))

    def test_staffing_allocation_and_removal_are_audited_as_project_team_operations(self):
        project = Project.objects.create(
            organization=self.org, created_by=self.owner, name="Audited team project",
            status=Project.Status.ACTIVE,
        )
        draft = self.change("post", f"projects/{project.pk}/staffing-requests/", {
            "department_id": self.department.pk, "requested_count": 1, "description": "Private request",
        }, status=201)
        staffing = StaffingRequest.objects.get(pk=draft.json()["id"])
        self.assertNotIn("Private request", str(self.event("staffing_request.created", staffing.pk).changes))
        self.change("post", f"staffing-requests/{staffing.pk}/submit/")
        self.change("post", f"staffing-requests/{staffing.pk}/allocate/", {
            "department_membership_ids": [self.department_member.pk],
        })
        allocation = staffing.allocations.get()
        assigned = self.event("staffing_allocation.assigned", allocation.pk)
        self.assertEqual(assigned.project_id, project.pk)
        self.assertEqual(assigned.actor_id, self.owner.pk)
        self.assertEqual(assigned.operation_id, self.event("staffing_request.status_changed", staffing.pk).operation_id)
        membership = project.memberships.get(organization_membership=self.worker_member)
        self.change("delete", f"projects/{project.pk}/team/{membership.pk}/", status=204)
        self.assertEqual(self.event("project.member_removed", project.pk).actor_id, self.owner.pk)

    def test_calendar_series_move_audience_and_rsvp_keep_minimal_audit(self):
        created = self.change("post", "events/", {
            "title": "Audited calendar series", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
            "rsvp_mode": "required", "recurrence": {"frequency": "daily", "count": 2},
        }, status=201)
        event = CalendarEvent.objects.get(pk=created.json()["id"])
        self.assertEqual(self.event("calendar_event.created", event.pk).actor_id, self.owner.pk)
        self.change("post", f"events/{event.pk}/invitations/", {"member_ids": [self.worker_member.pk]}, status=201)
        invited = self.event("calendar_event.invitations_sent", event.pk)
        self.assertEqual(invited.metadata["invitation_count"], 1)
        self.assertNotIn("member_ids", invited.metadata)

        key = "2027-11-16"
        self.change("patch", f"events/{event.pk}/occurrences/{key}/", {
            "expected_version": 0, "starts_at": "2027-11-16T18:00:00+00:00",
            "ends_at": "2027-11-16T19:00:00+00:00",
        })
        moved = self.event("calendar_event.occurrence_updated", event.pk)
        self.assertEqual(moved.metadata["occurrence_key"], key)
        self.assertEqual(datetime.fromisoformat(moved.changes["starts_at"]["old"]).hour, 16)
        self.assertEqual(datetime.fromisoformat(moved.changes["starts_at"]["new"]).hour, 18)

        invitation = event.invitations.get(recipient=self.worker_member)
        self.client.force_login(self.worker)
        response = write(self.client, "post", f"/api/v1/organizations/calendar/invitations/{invitation.pk}/respond/", {
            "response": "yes", "expected_revision": event.rsvp_revision, "expected_response_at": None,
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(invitation.response_history.count(), 1)
        row = self.event("calendar_event.rsvp_recorded", event.pk)
        self.assertEqual(row.metadata, {"history_id": invitation.response_history.get().pk})
        self.assertEqual(row.changes, {})
        self.assertEqual(row.actor_id, self.worker.pk)

        grant(self.org, self.reader_member, "calendar.read", "audit.read_all", "audit.read_details")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(f"{self.audit}{row.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"{self.audit}{invited.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"{self.audit}{moved.pk}/").status_code, 200)

    def test_notification_policy_is_audited_and_personal_preferences_are_not(self):
        self.change("patch", "notification-policy/", {"task_offsets_minutes": [1440, 60]})
        row = AuditEvent.objects.get(organization=self.org, action="notification_policy.updated")
        self.assertEqual(row.changes["task_offsets_minutes"], {"old": [], "new": [1440, 60]})
        self.assertEqual(row.actor_id, self.owner.pk)
        before = AuditEvent.objects.filter(organization=self.org).count()
        self.client.force_login(self.worker)
        self.change("patch", "notification-preferences/", {"quiet_start": "22:00", "quiet_end": "08:00"})
        self.assertEqual(AuditEvent.objects.filter(organization=self.org).count(), before)

    def test_ordinary_participant_gets_only_sanitized_current_object_history(self):
        task = Task.objects.create(
            organization=self.org, created_by=self.owner_member, assignee=self.worker_member,
            name="Personal audit history",
        )
        hidden = Task.objects.create(
            organization=self.org, created_by=self.owner_member, name="Hidden history task",
        )
        for target in (task, hidden):
            self.change("patch", f"tasks/{target.pk}/", {"due_at": "2027-11-10T14:00:00+00:00"})
        row = self.event("task.updated", task.pk)
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(self.audit).status_code, 403)
        self.assertEqual(self.client.get(f"{self.audit}{row.pk}/").status_code, 403)
        history_url = f"{self.audit}objects/task/{task.pk}/"
        history = self.client.get(history_url)
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(history.json()["total"], 1)
        item = history.json()["items"][0]
        self.assertEqual(item["id"], row.pk)
        self.assertFalse(item["can_view_details"])
        self.assertIsNone(item["actor_id"])
        self.assertIsNone(item["actor_name"])
        self.assertNotIn("changes", item)
        filtered = self.client.get(history_url, {"actor_id": self.owner.pk})
        self.assertEqual(filtered.json()["total"], 0)
        self.assertEqual(self.client.get(f"{self.audit}objects/task/{hidden.pk}/").status_code, 404)
        Task.objects.filter(pk=task.pk).update(assignee=None)
        self.assertEqual(self.client.get(history_url).status_code, 404)
