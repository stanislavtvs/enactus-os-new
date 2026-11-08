from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task, TaskCollaborator
from apps.tasks.services import TaskConflict, set_assignee, set_status
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="tasks-owner")
        cls.foreign_owner = users.create_superuser(username="tasks-foreign-owner")
        cls.author = users.create_user(username="tasks-author")
        cls.lead = users.create_user(username="tasks-project-lead")
        cls.assignee = users.create_user(username="tasks-assignee")
        cls.collaborator = users.create_user(username="tasks-collaborator")
        cls.outsider = users.create_user(username="tasks-outsider")
        cls.inactive = users.create_user(username="tasks-inactive")
        cls.org = create_organization(user=cls.owner, name="Tasks workspace")
        cls.foreign_org = create_organization(user=cls.foreign_owner, name="Foreign tasks workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign_org.memberships.get(user=cls.foreign_owner)

        def add_member(user, **kwargs):
            return OrganizationMembership.objects.create(organization=cls.org, user=user, **kwargs)

        cls.author_member = add_member(cls.author)
        cls.lead_member = add_member(cls.lead)
        cls.assignee_member = add_member(cls.assignee)
        cls.collaborator_member = add_member(cls.collaborator)
        cls.outsider_member = add_member(cls.outsider)
        cls.inactive_member = add_member(cls.inactive, is_active=False)
        cls.department = Department.objects.create(organization=cls.org, name="Tasks department")
        cls.child_department = Department.objects.create(
            organization=cls.org, name="Task branch", parent=cls.department,
        )
        cls.other_department = Department.objects.create(organization=cls.org, name="Other department")
        cls.foreign_department = Department.objects.create(
            organization=cls.foreign_org, name="Foreign department",
        )
        cls.project = Project.objects.create(
            organization=cls.org, name="Tasks project", lead_membership=cls.lead_member,
            responsible_department=cls.department, created_by=cls.owner, status=Project.Status.ACTIVE,
        )
        cls.foreign_project = Project.objects.create(
            organization=cls.foreign_org, name="Foreign project", created_by=cls.foreign_owner,
        )
        for member in (cls.assignee_member, cls.collaborator_member):
            ProjectMembership.objects.create(
                organization=cls.org, project=cls.project, organization_membership=member,
            )
        cls.standalone = Task.objects.create(
            organization=cls.org, name="Standalone task", created_by=cls.author_member,
            department=cls.department,
        )
        cls.project_task = Task.objects.create(
            organization=cls.org, name="Project task", created_by=cls.author_member,
            project=cls.project, department=cls.department,
        )
        cls.foreign_task = Task.objects.create(
            organization=cls.foreign_org, name="Foreign task", created_by=cls.foreign_member,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.task_list = f"{self.org_url}tasks/"

    def task_url(self, task, suffix=""):
        return f"{self.task_list}{task.pk}/{suffix}"

    def enable_direct_assignment(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")

    def change_assignee(self, task, member, *, csrf=True):
        return write(
            self.client, "put", self.task_url(task, "assignee/"),
            {"assignee_id": member.pk if member else None}, csrf=csrf,
        )

    def change_status(self, task, status, *, csrf=True):
        return write(
            self.client, "put", self.task_url(task, "status/"), {"status": status}, csrf=csrf,
        )

    def change_collaborator(self, method, task, member):
        return write(
            self.client, method, self.task_url(task, f"collaborators/{member.pk}/"),
        )

    def test_create_task_with_timezones_and_validate_dates_and_workflow_fields(self):
        self.client.force_login(self.owner)
        start = "2026-11-10T09:00:00+05:00"
        due = "2026-11-10T11:00:00+05:00"
        response = write(
            self.client, "post", self.task_list,
            {"name": "  New task  ", "description": "Description", "priority": "high",
             "planned_start_at": start, "due_at": due},
        )
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(task.name, "New task")
        self.assertEqual(task.status, Task.Status.PENDING)
        self.assertEqual(task.priority, Task.Priority.HIGH)
        self.assertEqual(task.created_by_id, self.owner_member.pk)
        self.assertIsNone(task.assignee_id)
        self.assertEqual(task.planned_start_at, datetime.fromisoformat(start))
        self.assertEqual(task.due_at, datetime.fromisoformat(due))

        for payload in (
            {"due_at": "2026-11-10T07:00:00+05:00"},
            {"due_at": "2026-11-10T11:00:00"},
            {"status": "completed"},
            {"completed_at": due},
            {"priority": "urgent"},
            {"assignee_id": self.assignee_member.pk},
            {"project_id": self.project.pk},
        ):
            with self.subTest(payload=payload):
                response = write(self.client, "patch", self.task_url(task), payload)
                self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(
            write(self.client, "post", self.task_list, {"name": "No bypass", "assignee_id": self.assignee_member.pk})
            .status_code,
            422,
        )
        task.refresh_from_db()
        self.assertEqual(task.due_at, datetime.fromisoformat(due))
        self.assertEqual(task.status, Task.Status.PENDING)
        self.assertIsNone(task.assignee_id)

    def test_direct_assignment_is_opt_in_and_rejects_wrong_tenant_or_inactive_member(self):
        self.client.force_login(self.owner)
        self.org.refresh_from_db()
        self.assertEqual(self.org.task_assignment_mode, Organization.TaskAssignmentMode.DEPARTMENT)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 409)
        self.standalone.refresh_from_db()
        self.assertIsNone(self.standalone.assignee_id)

        self.enable_direct_assignment()
        self.assertEqual(self.change_assignee(self.standalone, self.foreign_member).status_code, 404)
        self.assertEqual(self.change_assignee(self.standalone, self.inactive_member).status_code, 409)
        response = self.change_assignee(self.standalone, self.assignee_member)
        self.assertEqual(response.status_code, 200, response.content)
        self.standalone.refresh_from_db()
        self.assertEqual(self.standalone.assignee_id, self.assignee_member.pk)
        self.assertEqual(self.change_assignee(self.standalone, None).status_code, 200)
        self.standalone.refresh_from_db()
        self.assertIsNone(self.standalone.assignee_id)

    def test_project_assignment_needs_actual_lead_and_active_project_membership(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_assignee(self.project_task, self.assignee_member).status_code, 403)

        self.client.force_login(self.lead)
        grant(self.org, self.lead_member, "projects.view", "tasks.view")
        self.assertEqual(self.change_assignee(self.project_task, self.assignee_member).status_code, 403)

        grant(self.org, self.lead_member, "tasks.assign")
        self.assertEqual(self.change_assignee(self.project_task, self.outsider_member).status_code, 409)
        self.assertEqual(self.change_assignee(self.project_task, self.assignee_member).status_code, 200)
        self.project_task.refresh_from_db()
        self.assertEqual(self.project_task.assignee_id, self.assignee_member.pk)

    def test_author_primary_and_collaborator_read_without_inheriting_task_actions(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)
        self.assertEqual(
            self.change_collaborator("put", self.standalone, self.collaborator_member).status_code,
            204,
        )
        self.assertEqual(
            self.change_collaborator("put", self.standalone, self.collaborator_member).status_code,
            409,
        )
        self.assertEqual(self.standalone.collaborators.count(), 1)
        for user in (self.author, self.assignee, self.collaborator):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.task_url(self.standalone))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    write(self.client, "patch", self.task_url(self.standalone), {"name": "Unauthorized"})
                    .status_code,
                    403,
                )
                self.assertEqual(
                    self.change_status(self.standalone, Task.Status.IN_PROGRESS).status_code,
                    403,
                )

        self.client.force_login(self.owner)
        self.assertEqual(
            self.change_collaborator("delete", self.standalone, self.collaborator_member).status_code,
            204,
        )
        self.assertFalse(self.standalone.collaborators.get().is_active)
        self.client.force_login(self.collaborator)
        self.assertEqual(self.client.get(self.task_url(self.standalone)).status_code, 404)

    def test_project_view_is_required_and_project_members_do_not_inherit_task_access(self):
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.task_url(self.project_task)).status_code, 404)
        self.client.force_login(self.collaborator)
        self.assertEqual(self.client.get(f"{self.org_url}projects/{self.project.pk}/").status_code, 200)
        self.assertEqual(self.client.get(self.task_url(self.project_task)).status_code, 404)

        self.enable_direct_assignment()
        grant(self.org, self.lead_member, "projects.view", "tasks.view", "tasks.assign")
        self.client.force_login(self.lead)
        self.assertEqual(
            self.change_collaborator("put", self.project_task, self.collaborator_member).status_code,
            204,
        )
        self.client.force_login(self.collaborator)
        self.assertEqual(self.client.get(self.task_url(self.project_task)).status_code, 200)

        ProjectMembership.objects.filter(
            project=self.project, organization_membership=self.collaborator_member,
        ).update(is_active=False)
        self.assertEqual(self.client.get(self.task_url(self.project_task)).status_code, 404)
        self.assertFalse(self.project_task.collaborators.get().is_active)

    def test_assignee_and_collaborator_roles_cannot_overlap(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)
        self.assertEqual(
            self.change_collaborator("put", self.standalone, self.assignee_member).status_code, 409
        )
        self.assertEqual(
            self.change_collaborator("put", self.standalone, self.collaborator_member).status_code,
            204,
        )
        self.assertEqual(
            self.change_assignee(self.standalone, self.collaborator_member).status_code, 409
        )
        self.standalone.refresh_from_db()
        self.assertEqual(self.standalone.assignee_id, self.assignee_member.pk)
        self.assertEqual(
            self.standalone.collaborators.get().organization_membership_id,
            self.collaborator_member.pk,
        )

    def test_task_options_do_not_expose_candidates_without_assignment_authority(self):
        url = f"{self.org_url}task-options/"
        query = {"task_id": self.project_task.pk}
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url, query).json()["assignees"], [])

        grant(self.org, self.lead_member, "projects.view", "tasks.view", "tasks.assign")
        self.client.force_login(self.lead)
        response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {row["id"] for row in response.json()["assignees"]},
            {self.assignee_member.pk, self.collaborator_member.pk},
        )
        self.assertEqual(self.client.get(url, {"task_id": self.foreign_task.pk}).status_code, 404)

    def test_status_workflow_requires_primary_and_records_completion(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_status(self.standalone, Task.Status.IN_PROGRESS).status_code, 409)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)
        self.assertEqual(self.change_status(self.standalone, Task.Status.COMPLETED).status_code, 409)
        for status in (
            Task.Status.IN_PROGRESS, Task.Status.REVIEW, Task.Status.IN_PROGRESS,
            Task.Status.REVIEW, Task.Status.COMPLETED,
        ):
            with self.subTest(status=status):
                response = self.change_status(self.standalone, status)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["status"], status)
        self.standalone.refresh_from_db()
        self.assertEqual(self.standalone.status, Task.Status.COMPLETED)
        self.assertIsNotNone(self.standalone.completed_at)
        self.assertTrue(self.standalone.completed_at.utcoffset() is not None)
        self.assertEqual(self.change_status(self.standalone, Task.Status.IN_PROGRESS).status_code, 409)

    def test_deactivation_unassigns_active_tasks_and_preserves_completed_history(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)
        self.assertEqual(self.change_status(self.standalone, Task.Status.IN_PROGRESS).status_code, 200)
        historical = Task.objects.create(
            organization=self.org, name="Completed task", created_by=self.owner_member,
            assignee=self.assignee_member, status=Task.Status.COMPLETED,
            completed_at=datetime.fromisoformat("2026-11-10T08:00:00+00:00"),
        )

        OrganizationMembership.objects.filter(pk=self.assignee_member.pk).update(is_active=False)

        self.standalone.refresh_from_db()
        historical.refresh_from_db()
        self.assertIsNone(self.standalone.assignee_id)
        self.assertEqual(self.standalone.status, Task.Status.PENDING)
        self.assertEqual(historical.assignee_id, self.assignee_member.pk)
        self.assertEqual(historical.status, Task.Status.COMPLETED)

    def test_task_move_needs_authority_at_source_and_destination(self):
        grant(self.org, self.author_member, "tasks.view", "tasks.update", department=self.department)
        self.client.force_login(self.author)
        url = self.task_url(self.standalone)
        self.assertEqual(write(self.client, "patch", url, {"name": "Renamed"}).status_code, 200)
        response = write(self.client, "patch", url, {"department_id": self.other_department.pk})
        self.assertEqual(response.status_code, 403, response.content)

        grant(self.org, self.author_member, "tasks.view", "tasks.update", department=self.other_department)
        response = write(self.client, "patch", url, {"department_id": self.other_department.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.standalone.refresh_from_db()
        self.assertEqual(self.standalone.department_id, self.other_department.pk)

    def test_list_filters_pagination_and_archive_preserve_task_data(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)
        archived = Task.objects.create(
            organization=self.org, name="Archived standalone task", created_by=self.owner_member,
            department=self.department,
        )
        self.assertEqual(write(self.client, "delete", self.task_url(archived)).status_code, 204)
        response = self.client.get(self.task_list, {"page": 1, "page_size": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"items", "total", "page", "page_size"})
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["items"]), 1)
        next_page = self.client.get(self.task_list, {"page": 2, "page_size": 1}).json()
        self.assertNotEqual(next_page["items"][0]["id"], body["items"][0]["id"])

        filters = {
            "q": "standalone", "status": "pending", "department_id": self.department.pk,
            "include_archived": "true",
        }
        response = self.client.get(self.task_list, filters)
        self.assertEqual(
            {task["id"] for task in response.json()["items"]}, {self.standalone.pk, archived.pk}
        )
        response = self.client.get(self.task_list, {"project_id": self.project.pk})
        self.assertEqual([row["id"] for row in response.json()["items"]], [self.project_task.pk])
        self.assertEqual(write(self.client, "patch", self.task_url(archived), {"name": "No"}).status_code, 409)
        self.assertEqual(self.client.get(self.task_url(archived)).status_code, 200)

        self.client.force_login(self.assignee)
        response = self.client.get(self.task_list, {"mine": "true"})
        self.assertEqual([row["id"] for row in response.json()["items"]], [self.standalone.pk])

    def test_tasks_reject_anonymous_csrf_and_cross_tenant_requests(self):
        self.assertEqual(self.client.get(self.task_list).status_code, 401)
        self.assertEqual(self.client.get(self.task_url(self.standalone)).status_code, 401)
        for user in (self.foreign_owner, self.inactive):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.task_list).status_code, 404)
            self.assertEqual(self.client.get(self.task_url(self.standalone)).status_code, 404)

        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.task_url(self.foreign_task)).status_code, 404)
        for method, url, data in (
            ("post", self.task_list, {"name": "No CSRF"}),
            ("patch", self.task_url(self.standalone), {"name": "No CSRF"}),
            ("delete", self.task_url(self.standalone), None),
            ("put", self.task_url(self.standalone, "status/"), {"status": "cancelled"}),
        ):
            with self.subTest(method=method):
                self.assertEqual(write(self.client, method, url, data, csrf=False).status_code, 403)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member, csrf=False).status_code, 403)
        for fields in (
            {"department_id": self.foreign_department.pk},
            {"project_id": self.foreign_project.pk},
        ):
            self.assertEqual(
                write(self.client, "post", self.task_list, {"name": "Foreign", **fields}).status_code,
                404,
            )

    def test_task_assignment_mode_can_only_be_enabled_by_owner_with_csrf(self):
        url = reverse("organization-settings", kwargs={"org_id": self.org.pk})
        payload = {"action": "task_assignment_mode", "task_mode": "direct"}

        def csrf_headers():
            self.client.get(reverse("login"))
            return {"X-CSRFToken": self.client.cookies[settings.CSRF_COOKIE_NAME].value}

        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url, payload, headers=csrf_headers()).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, payload).status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual(self.org.task_assignment_mode, Organization.TaskAssignmentMode.DEPARTMENT)

        response = self.client.post(url, payload, headers=csrf_headers())
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertEqual(self.org.task_assignment_mode, Organization.TaskAssignmentMode.DIRECT)
        self.assertEqual(self.change_assignee(self.standalone, self.assignee_member).status_code, 200)

    def test_tasks_pages_and_sidebar_respect_read_permissions(self):
        org_args = {"org_id": self.org.pk}
        task_list = reverse("organization-tasks", kwargs=org_args)
        mine = reverse("organization-my-tasks", kwargs=org_args)
        detail = reverse("organization-task", kwargs={**org_args, "task_id": self.project_task.pk})
        project_tasks = reverse("project-tasks", kwargs={**org_args, "project_id": self.project.pk})
        self.assertRedirects(
            self.client.get(task_list), f"{reverse('login')}?next={task_list}",
            fetch_redirect_response=False,
        )
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(task_list).status_code, 404)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(self.client.get(project_tasks).status_code, 404)
        response = self.client.get(mine)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["mine"])
        self.assertNotContains(response, f'href="{task_list}"')

        self.client.force_login(self.owner)
        response = self.client.get(task_list)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["mine"])
        self.assertContains(response, f'href="{mine}"')
        response = self.client.get(project_tasks)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["project_id"], self.project.pk)
        self.assertEqual(self.client.get(detail).context["task_id"], self.project_task.pk)

    def test_postgresql_rejects_cross_tenant_tasks_and_collaborators_in_bulk(self):
        invalid_rows = [
            Task(organization=self.org, name="Foreign author", created_by=self.foreign_member),
            Task(organization=self.org, name="Foreign project", created_by=self.owner_member,
                 project=self.foreign_project),
            Task(organization=self.org, name="Foreign department", created_by=self.owner_member,
                 department=self.foreign_department),
            Task(organization=self.org, name="Foreign assignee", created_by=self.owner_member,
                 assignee=self.foreign_member),
            TaskCollaborator(organization=self.org, task=self.standalone,
                             organization_membership=self.foreign_member),
            TaskCollaborator(organization=self.org, task=self.foreign_task,
                             organization_membership=self.collaborator_member),
        ]
        for row in invalid_rows:
            with self.subTest(model=type(row).__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    type(row).objects.bulk_create([row])
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class TaskConcurrencyTests(TransactionTestCase):
    def test_concurrent_unassign_and_start_never_leave_a_working_task_without_primary(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="task-race-owner")
        worker = users.create_user(username="task-race-worker")
        organization = create_organization(user=owner, name="Task race")
        Organization.objects.filter(pk=organization.pk).update(task_assignment_mode="direct")
        membership = OrganizationMembership.objects.create(organization=organization, user=worker)
        task = Task.objects.create(
            organization=organization, name="Race task", created_by=organization.memberships.get(user=owner),
            assignee=membership,
        )
        start = Barrier(2, timeout=5)

        def change(operation, fields):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    operation(user=owner, org_id=organization.pk, task_id=task.pk, **fields)
            except TaskConflict:
                return "conflict"
            finally:
                worker_connection.close()
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            unassign = pool.submit(change, set_assignee, {"assignee_id": None})
            begin = pool.submit(change, set_status, {"status": Task.Status.IN_PROGRESS})
            self.assertCountEqual(
                [unassign.result(timeout=12), begin.result(timeout=12)], ["success", "conflict"]
            )
        task.refresh_from_db()
        self.assertIn(
            (task.status, task.assignee_id),
            {(Task.Status.PENDING, None), (Task.Status.IN_PROGRESS, membership.pk)},
        )
