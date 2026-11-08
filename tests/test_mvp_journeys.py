from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import Organization, OrganizationMembership
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class MVPJourneys(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="mvp-journey-owner")
        cls.foreign_owner = users.create_superuser(username="mvp-journey-foreign")
        cls.lead = users.create_user(username="mvp-journey-lead")
        cls.allocator = users.create_user(username="mvp-journey-allocator")
        cls.worker = users.create_user(username="mvp-journey-worker")
        cls.collaborator = users.create_user(username="mvp-journey-collaborator")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)

    def change(self, method, url, payload=None, *, status=200):
        result = write(self.client, method, url, payload)
        self.assertEqual(result.status_code, status, result.content)
        return result.json() if result.content else None

    def read(self, url, params=None, *, status=200):
        result = self.client.get(url, params)
        self.assertEqual(result.status_code, status, result.content)
        return result.json() if status == 200 else None

    def org(self, name):
        created = self.change("post", API, {"name": name}, status=201)
        return Organization.objects.get(pk=created["id"])

    def form(self, url, payload):
        self.client.get(reverse("login"))
        csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        return self.client.post(url, payload, headers={"X-CSRFToken": csrf})

    def member(self, org, user):
        return OrganizationMembership.objects.create(organization=org, user=user)

    def test_organization_onboards_member_and_limits_delegated_role_to_branch(self):
        org = self.org("MVP organization journey")
        root = f"{API}{org.pk}/"
        self.assertEqual(org.owner_id, self.owner.pk)
        self.assertTrue(org.memberships.get(user=self.owner).is_active)
        branch = self.change("post", f"{root}departments/", {"name": "Design"}, status=201)
        child = self.change("post", f"{root}departments/", {
            "name": "Content", "parent_id": branch["id"],
        }, status=201)
        sibling = self.change("post", f"{root}departments/", {"name": "Logistics"}, status=201)
        invitation = self.change("post", f"{root}invitations/", {"username": self.lead.username}, status=201)
        self.client.force_login(self.lead)
        self.read(root, status=404)
        self.change("post", f"{API}invitations/respond/", {"invitation_id": invitation["id"], "decision": "accept"})
        self.assertEqual(self.read(root)["id"], org.pk)
        membership = org.memberships.get(user=self.lead)

        self.client.force_login(self.owner)
        invited = self.change("post", f"{root}departments/{branch['id']}/invitations/", {
            "organization_membership_id": membership.pk,
        }, status=201)
        self.client.force_login(self.lead)
        self.change("post", f"{API}department-invitations/{invited['id']}/respond/", {"decision": "accept"})
        scope = DepartmentMembership.objects.get(organization_membership=membership, department_id=branch["id"])
        self.client.force_login(self.owner)
        role = self.change("post", f"{root}roles/", {
            "name": "Branch editor",
            "permissions": [{"code": "departments.update", "include_descendants": True}],
        }, status=201)
        self.change("post", f"{root}role-assignments/", {
            "role_id": role["id"], "organization_membership_id": membership.pk,
            "department_membership_id": scope.pk,
        }, status=201)

        self.client.force_login(self.lead)
        self.change("patch", f"{root}departments/{child['id']}/", {"name": "Editorial"})
        self.change("patch", f"{root}departments/{sibling['id']}/", {"name": "Denied"}, status=403)
        self.assertEqual(self.read(f"{root}departments/{sibling['id']}/")["name"], "Logistics")
        self.read(f"{root}audit/", status=403)

    def staffed_project(self):
        org = self.org("MVP project journey")
        root = f"{API}{org.pk}/"
        lead = self.member(org, self.lead)
        allocator = self.member(org, self.allocator)
        worker = self.member(org, self.worker)
        collaborator = self.member(org, self.collaborator)
        primary = Department.objects.get(pk=self.change(
            "post", f"{root}departments/", {"name": "Production"}, status=201,
        )["id"])
        support = self.change("post", f"{root}departments/", {"name": "Support"}, status=201)
        sources = [
            DepartmentMembership.objects.create(
                organization=org, department=primary, organization_membership=membership,
            )
            for membership in (worker, collaborator)
        ]
        grant(
            org, lead, "projects.view", "projects.create", "projects.manage_departments",
            "projects.manage_lead", "projects.staffing.request", "tasks.view", "tasks.create",
            "tasks.update", "tasks.assign", "tasks.change_status", "tasks.review.approve",
        )
        grant(org, allocator, "departments.staffing.allocate", department=primary)
        self.client.force_login(self.lead)
        project = self.change("post", f"{root}projects/", {
            "name": "MVP pilot", "status": "active", "lead_membership_id": lead.pk,
        }, status=201)
        project_url = f"{root}projects/{project['id']}/"
        self.change("put", f"{project_url}responsible-department/", {"responsible_department_id": primary.pk})
        self.change("put", f"{project_url}departments/{support['id']}/", status=204)
        draft = self.change("post", f"{project_url}staffing-requests/", {
            "department_id": primary.pk, "requested_count": 2, "description": "Build the pilot",
        }, status=201)
        staffing_url = f"{root}staffing-requests/{draft['id']}/"
        submitted = self.change("post", f"{staffing_url}submit/")
        self.assertEqual(submitted["status"], "sent")
        self.client.force_login(self.allocator)
        self.read(project_url, status=404)
        result = self.change("post", f"{staffing_url}allocate/", {
            "department_membership_ids": [item.pk for item in sources],
        })
        self.assertEqual(result["status"], "fulfilled")
        return org, root, Project.objects.get(pk=project["id"]), primary, worker, collaborator, sources

    def test_project_lead_and_department_allocator_staff_project_without_moving_people(self):
        org, root, project, primary, worker, collaborator, sources = self.staffed_project()
        self.assertEqual(project.responsible_department_id, primary.pk)
        self.assertTrue(project.department_links.exclude(department_id=primary.pk).exists())
        self.assertCountEqual(project.memberships.filter(is_active=True).values_list("organization_membership_id", flat=True), [
            worker.pk, collaborator.pk,
        ])
        self.assertEqual(DepartmentMembership.objects.filter(
            pk__in=[source.pk for source in sources], department=primary, is_active=True,
        ).count(), 2)
        self.client.force_login(self.worker)
        self.assertEqual(self.read(f"{root}projects/{project.pk}/")["id"], project.pk)
        self.change("patch", f"{root}projects/{project.pk}/", {"name": "Denied"}, status=403)

    def enable_direct(self, org):
        result = self.form(reverse("organization-settings", args=[org.pk]), {
            "action": "task_assignment_mode", "task_mode": "direct",
        })
        self.assertEqual(result.status_code, 302, result.content)

    def decide_current(self, task_url, outcome="approved"):
        workflow = self.read(f"{task_url}workflow/")["review"]
        self.assertIsNotNone(workflow["current_stage_id"])
        self.change("post", f"{task_url}review/decisions/", {
            "stage_id": workflow["current_stage_id"], "outcome": outcome,
            "comment": "Revise the draft" if outcome == "returned" else "",
        }, status=204)

    def submit_result(self, task_url, comment):
        self.change("post", f"{task_url}review/submissions/", {"comment": comment, "links": []}, status=204)

    def test_task_dependency_and_two_stage_review_survive_return_and_resubmission(self):
        org, root, project, primary, worker, collaborator, _ = self.staffed_project()
        self.client.force_login(self.owner)
        self.enable_direct(org)
        grant(org, worker, "tasks.review.submit")
        prereq = self.change("post", f"{root}tasks/", {
            "name": "Prepare inputs", "department_id": primary.pk, "assignee_id": worker.pk,
        }, status=201)
        prereq_url = f"{root}tasks/{prereq['id']}/"
        self.client.force_login(self.lead)
        task = self.change("post", f"{root}tasks/", {
            "name": "Produce pilot", "department_id": primary.pk, "project_id": project.pk,
            "assignee_id": worker.pk,
        }, status=201)
        task_url = f"{root}tasks/{task['id']}/"
        self.change("put", f"{task_url}collaborators/{collaborator.pk}/", status=204)
        child = self.change("post", f"{task_url}subtasks/", {"name": "Draft slides"}, status=201)
        self.assertEqual(child["parent_id"], task["id"])
        checklist = self.change("post", f"{task_url}checklists/", {"name": "Release checks"}, status=201)
        self.change("post", f"{task_url}checklists/{checklist['id']}/items/", {"name": "Verify title"}, status=201)
        self.change("post", f"{task_url}dependencies/", {
            "predecessor_id": prereq["id"], "kind": "strict",
        }, status=201)
        self.change("put", f"{task_url}status/", {"status": "in_progress"}, status=409)

        self.client.force_login(self.owner)
        self.change("put", f"{task_url}review/policy/", {
            "policy": "department_project", "reviewer_id": None,
        }, status=204)
        self.change("put", f"{prereq_url}status/", {"status": "in_progress"})
        self.client.force_login(self.worker)
        self.submit_result(prereq_url, "Inputs ready")
        self.client.force_login(self.owner)
        self.decide_current(prereq_url)
        self.assertEqual(self.read(prereq_url)["status"], "completed")

        self.client.force_login(self.lead)
        self.change("put", f"{task_url}status/", {"status": "in_progress"})
        self.client.force_login(self.worker)
        self.submit_result(task_url, "First draft")
        self.client.force_login(self.owner)
        overview = self.client.get(f"/organizations/{org.pk}/")
        self.assertContains(overview, "Ожидает вашей проверки")
        self.assertContains(overview, "Produce pilot")
        self.decide_current(task_url, "returned")
        self.assertEqual(self.read(task_url)["status"], "in_progress")
        self.client.force_login(self.worker)
        self.submit_result(task_url, "Second draft")
        self.client.force_login(self.owner)
        self.decide_current(task_url)
        self.assertEqual(self.read(task_url)["status"], "review")
        self.client.force_login(self.lead)
        self.decide_current(task_url)
        self.assertEqual(self.read(task_url)["status"], "completed")

        self.client.force_login(self.owner)
        history = self.read(f"{root}audit/objects/task/{task['id']}/")
        actions = {row["action"] for row in history["items"]}
        self.assertTrue({
            "task.created", "task.assigned", "task.dependency_added",
            "task.review_submitted", "task.review_returned", "task.review_approved",
        } <= actions)

    def test_workspace_switch_changes_context_not_authority_and_revocation_redacts_notices(self):
        first = self.org("MVP first workspace")
        self.enable_direct(first)
        self.client.force_login(self.foreign_owner)
        second = self.org("MVP second workspace")
        self.enable_direct(second)
        first_member = self.member(first, self.worker)
        second_member = self.member(second, self.worker)
        grant(first, first_member, "projects.view", "calendar.read", "audit.read_all", "audit.read_details")

        self.client.force_login(self.owner)
        first_task = self.change("post", f"{API}{first.pk}/tasks/", {
            "name": "First visible assignment", "assignee_id": first_member.pk,
        }, status=201)
        first_project = self.change("post", f"{API}{first.pk}/projects/", {"name": "Visible workspace project"}, status=201)
        self.client.force_login(self.foreign_owner)
        second_task = self.change("post", f"{API}{second.pk}/tasks/", {
            "name": "Second confidential assignment", "assignee_id": second_member.pk,
        }, status=201)
        second_project = self.change("post", f"{API}{second.pk}/projects/", {"name": "Hidden workspace project"}, status=201)
        hidden_event = self.change("post", f"{API}{second.pk}/events/", {
            "title": "Hidden workspace event", "type": "meeting", "timezone": "UTC",
            "starts_at": "2027-11-15T16:00:00+00:00", "ends_at": "2027-11-15T17:00:00+00:00",
        }, status=201)

        self.client.force_login(self.worker)
        switch = reverse("organization-switch")
        for org in (first, second):
            result = self.form(switch, {"organization_id": org.pk})
            self.assertEqual(result.status_code, 302, result.content)
            self.assertEqual(self.client.session["active_workspace_id"], org.pk)
        self.assertEqual(self.read(f"{API}{first.pk}/projects/{first_project['id']}/")["id"], first_project["id"])
        self.read(f"{API}{second.pk}/projects/{second_project['id']}/", status=404)
        self.read(f"{API}{second.pk}/events/{hidden_event['id']}/", status=404)
        self.assertEqual(self.read(f"{API}{second.pk}/tasks/{second_task['id']}/")["id"], second_task["id"])
        self.assertEqual(self.read(f"{API}{first.pk}/tasks/{first_task['id']}/")["id"], first_task["id"])
        notices = self.read("/api/v1/notifications/")["items"]
        self.assertCountEqual([row["organization_id"] for row in notices], [first.pk, second.pk])
        second_notice = next(row for row in notices if row["organization_id"] == second.pk)
        self.assertEqual(self.client.session["active_workspace_id"], second.pk)

        OrganizationMembership.objects.filter(pk=second_member.pk).update(is_active=False)
        self.assertEqual(self.form(switch, {"organization_id": second.pk}).status_code, 404)
        self.read(f"{API}{second.pk}/tasks/{second_task['id']}/", status=404)
        self.read(f"{API}{second.pk}/audit/", status=404)
        self.read(f"/api/v1/notifications/{second_notice['id']}/link/", status=404)
        after = self.read("/api/v1/notifications/")["items"]
        redacted = next(row for row in after if row["id"] == second_notice["id"])
        self.assertFalse(redacted["available"])
        self.assertIsNone(redacted["organization_id"])
        self.assertNotIn(second_task["name"], str(redacted))
