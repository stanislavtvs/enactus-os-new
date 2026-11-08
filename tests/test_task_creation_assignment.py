from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task, TaskDistribution
from tests.test_collection_pages import CollectionMarkup
from tests.test_department_memberships import write
from tests.test_projects import grant


class TaskCreationAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="task-creation-owner")
        cls.foreign_owner = users.create_superuser(username="task-creation-foreign")
        cls.lead = users.create_user(username="task-creation-lead")
        cls.worker = users.create_user(username="task-creation-worker")
        cls.non_team = users.create_user(username="task-creation-nonteam")
        cls.inactive = users.create_user(username="task-creation-inactive")
        cls.org = create_organization(user=cls.owner, name="Task creation")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign task creation")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.lead_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.lead)
        cls.worker_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.worker)
        cls.non_team_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.non_team,
        )
        cls.inactive_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.inactive, is_active=False,
        )
        cls.department = Department.objects.create(organization=cls.org, name="Project department")
        cls.project = Project.objects.create(
            organization=cls.org, name="Creation project", created_by=cls.owner,
            lead_membership=cls.lead_member, responsible_department=cls.department,
        )
        ProjectMembership.objects.create(
            organization=cls.org, project=cls.project, organization_membership=cls.worker_member,
        )
        cls.parent = Task.objects.create(
            organization=cls.org, name="Creation parent", created_by=cls.owner_member,
            project=cls.project, department=cls.department,
        )
        cls.foreign_parent = Task.objects.create(
            organization=cls.foreign, name="Foreign parent", created_by=cls.foreign_member,
        )
        grant(
            cls.org, cls.lead_member, "projects.view", "tasks.view", "tasks.create",
            "tasks.update", "tasks.assign",
        )
        grant(cls.org, cls.non_team_member, "tasks.view", "tasks.create", "tasks.assign")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        base = f"/api/v1/organizations/{self.org.pk}/"
        self.tasks_url = f"{base}tasks/"
        self.options_url = f"{base}task-assignee-options/"

    def enable_direct(self):
        Organization.objects.filter(pk=self.org.pk).update(
            task_assignment_mode=Organization.TaskAssignmentMode.DIRECT,
        )

    def subtask_url(self, parent):
        return f"{self.tasks_url}{parent.pk}/subtasks/"

    def assert_assigned(self, response, actor):
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["assignee"]["id"], self.worker_member.pk)
        task = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(task.assignee_id, self.worker_member.pk)
        self.assertEqual(task.status, Task.Status.PENDING)
        history = task.distributions.get()
        self.assertEqual(history.status, TaskDistribution.Status.ASSIGNED)
        self.assertEqual(history.mode, Organization.TaskAssignmentMode.DIRECT)
        self.assertEqual(history.assignee_id, self.worker_member.pk)
        self.assertEqual(history.requested_by_id, actor.pk)
        self.assertEqual(history.resolved_by_id, actor.pk)
        self.assertIsNotNone(history.resolved_at)
        return task

    def assert_failed_creation(self, url, expected_status, *, csrf=True, **fields):
        task_count = Task.objects.count()
        history_count = TaskDistribution.objects.count()
        response = write(self.client, "post", url, {"name": "Rejected task", **fields}, csrf=csrf)
        self.assertEqual(response.status_code, expected_status, response.content)
        self.assertEqual(Task.objects.count(), task_count)
        self.assertEqual(TaskDistribution.objects.count(), history_count)

    def test_direct_standalone_creation_records_assignment_and_no_csrf_means_no_task(self):
        self.enable_direct()
        self.client.force_login(self.owner)
        self.assert_failed_creation(
            self.tasks_url, 403, csrf=False, assignee_id=self.worker_member.pk,
        )
        response = write(
            self.client, "post", self.tasks_url,
            {"name": "Assigned on creation", "assignee_id": self.worker_member.pk},
        )
        task = self.assert_assigned(response, self.owner_member)
        self.assertIsNone(task.project_id)
        self.assertIsNone(task.parent_id)

    def test_project_lead_creates_assigned_subtask_in_inherited_scope(self):
        self.enable_direct()
        self.client.force_login(self.lead)
        response = write(
            self.client, "post", self.subtask_url(self.parent),
            {"name": "Assigned child", "assignee_id": self.worker_member.pk},
        )
        child = self.assert_assigned(response, self.lead_member)
        self.assertEqual(child.parent_id, self.parent.pk)
        self.assertEqual(child.project_id, self.project.pk)
        self.assertEqual(child.department_id, self.department.pk)
        self.assertEqual(child.distributions.get().department_id, self.department.pk)
        self.parent.refresh_from_db()
        self.assertIsNone(self.parent.assignee_id)

    def test_denied_creations_leave_no_task_or_assignment_history(self):
        self.enable_direct()
        self.client.force_login(self.lead)
        for member, expected in (
            (self.non_team_member, 409), (self.inactive_member, 409), (self.foreign_member, 404),
        ):
            with self.subTest(member=member.pk):
                self.assert_failed_creation(
                    self.subtask_url(self.parent), expected, assignee_id=member.pk,
                )
        self.assert_failed_creation(
            self.subtask_url(self.foreign_parent), 404, assignee_id=self.worker_member.pk,
        )
        self.client.force_login(self.owner)
        self.assert_failed_creation(
            self.tasks_url, 403, project_id=self.project.pk, assignee_id=self.worker_member.pk,
        )
        self.assert_failed_creation(
            self.subtask_url(self.parent), 403, assignee_id=self.worker_member.pk,
        )

    def test_department_mode_rejects_inline_assignment_and_can_create_unassigned(self):
        self.client.force_login(self.owner)
        self.assert_failed_creation(self.tasks_url, 409, assignee_id=self.worker_member.pk)
        self.client.force_login(self.lead)
        self.assert_failed_creation(
            self.subtask_url(self.parent), 409, assignee_id=self.worker_member.pk,
        )
        response = write(
            self.client, "post", self.subtask_url(self.parent),
            {"name": "Unassigned child", "assignee_id": None},
        )
        self.assertEqual(response.status_code, 201, response.content)
        child = Task.objects.get(pk=response.json()["id"])
        self.assertIsNone(child.assignee_id)
        self.assertFalse(child.distributions.exists())

    def test_candidate_options_enforce_assignment_policy_and_exact_project_team(self):
        scope = {"parent_id": self.parent.pk}
        self.assertEqual(self.client.get(self.options_url, scope).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.options_url, scope).status_code, 404)

        self.client.force_login(self.non_team)
        self.assertEqual(
            self.client.get(self.options_url, {"project_id": self.project.pk}).status_code,
            404,
        )
        self.client.force_login(self.lead)
        response = self.client.get(self.options_url, scope)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            "assignment_mode": "department", "can_assign": False, "candidates": [],
        })

        self.enable_direct()
        response = self.client.get(self.options_url, scope)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {
            "assignment_mode": "direct", "can_assign": True,
            "candidates": [{"id": self.worker_member.pk, "username": self.worker.username}],
        })
        self.assertEqual(
            self.client.get(self.options_url, {"parent_id": self.foreign_parent.pk}).status_code,
            404,
        )
        ProjectMembership.objects.filter(
            project=self.project, organization_membership=self.worker_member,
        ).update(is_active=False, ended_at=timezone.now())
        self.assert_failed_creation(
            self.subtask_url(self.parent), 409, assignee_id=self.worker_member.pk,
        )
        self.assertEqual(self.client.get(self.options_url, scope).json()["candidates"], [])
        self.client.force_login(self.owner)
        response = self.client.get(self.options_url, scope)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            "assignment_mode": "direct", "can_assign": False, "candidates": [],
        })

    def test_create_permission_without_assign_does_not_expose_or_accept_candidates(self):
        self.enable_direct()
        grant(self.org, self.worker_member, "tasks.view", "tasks.create")
        self.client.force_login(self.worker)
        response = self.client.get(self.options_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            "assignment_mode": "direct", "can_assign": False, "candidates": [],
        })
        self.assert_failed_creation(self.tasks_url, 403, assignee_id=self.non_team_member.pk)
        response = write(self.client, "post", self.tasks_url, {"name": "Unassigned allowed"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()["assignee"])

    def test_creation_dialogs_keep_assignee_fields_disabled_until_policy_loads(self):
        self.client.force_login(self.lead)
        for route, args, prefix, form_id, dialog_id in (
            (
                "organization-tasks", [self.org.pk], "task-create",
                "task-create-form", "task-create-panel",
            ),
            (
                "organization-task", [self.org.pk, self.parent.pk], "subtask",
                "task-subtask-form", "task-subtask-dialog",
            ),
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                self.assertEqual(response.status_code, 200, response.content)
                markup = CollectionMarkup(response)
                field_id = f"{prefix}-assignee"
                field_tag, field = markup.ids[field_id]
                self.assertEqual(field_tag, "select")
                self.assertIn("disabled", field)
                self.assertIn(field_id, markup.labels)
                hint = markup.ids[field["aria-describedby"]][1]
                self.assertEqual(hint["role"], "status")
                containing_dialog, fields = markup.forms[form_id]
                self.assertEqual(containing_dialog, dialog_id)
                self.assertIn("assignee_id", fields)
                self.assertEqual(markup.ids[dialog_id][0], "dialog")
                self.assertNotIn("open", markup.ids[dialog_id][1])
