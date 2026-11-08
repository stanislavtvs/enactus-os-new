from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase

from apps.departments.models import Department
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project
from apps.tasks.models import Checklist, ChecklistConversion, Task, TaskDependency
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskDependencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="dependency-owner")
        cls.foreign_owner = users.create_superuser(username="dependency-foreign-owner")
        cls.reader = users.create_user(username="dependency-reader")
        cls.worker = users.create_user(username="dependency-worker")
        cls.org = create_organization(user=cls.owner, name="Dependency workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign dependencies")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.reader_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.reader)
        cls.worker_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.worker)
        cls.department = Department.objects.create(organization=cls.org, name="Execution department")
        cls.other_department = Department.objects.create(organization=cls.org, name="Private department")
        cls.task = Task.objects.create(
            organization=cls.org, name="Dependent task", created_by=cls.owner_member,
            department=cls.department, assignee=cls.worker_member,
        )
        cls.predecessor = Task.objects.create(
            organization=cls.org, name="Private predecessor", created_by=cls.owner_member,
            department=cls.other_department, assignee=cls.worker_member,
        )
        cls.foreign_task = Task.objects.create(
            organization=cls.foreign, name="Foreign task", created_by=cls.foreign_member,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.tasks_url = f"{API}{self.org.pk}/tasks/"

    def task_url(self, task, suffix=""):
        return f"{self.tasks_url}{task.pk}/{suffix}"

    def add(self, task, predecessor, kind="strict", *, csrf=True):
        return write(
            self.client, "post", self.task_url(task, "dependencies/"),
            {"predecessor_id": predecessor.pk, "kind": kind}, csrf=csrf,
        )

    def dependency_data(self, task):
        response = self.client.get(self.task_url(task, "dependencies/"))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def status(self, task, status):
        return write(self.client, "put", self.task_url(task, "status/"), {"status": status})

    def submit(self, task):
        return write(
            self.client, "post", self.task_url(task, "review/submissions/"),
            {"comment": "Ready for review", "links": []},
        )

    def approve(self, task):
        stage = task.review_submissions.get().stages.get()
        return write(
            self.client, "post", self.task_url(task, "review/decisions/"),
            {"stage_id": stage.pk, "outcome": "approved", "comment": ""},
        )

    def make_task(self, name="Another task", **fields):
        return Task.objects.create(
            organization=self.org, name=name, created_by=self.owner_member, **fields,
        )

    def test_informational_predecessor_warns_without_blocking_start(self):
        self.client.force_login(self.owner)
        response = self.add(self.task, self.predecessor, "informational")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["predecessor"]["id"], self.predecessor.pk)
        data = self.dependency_data(self.task)
        self.assertFalse(data["blocked"])
        self.assertTrue(data["items"][0]["unresolved"])
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 200)

    def test_strict_dependency_blocks_old_status_and_pending_review_until_approved(self):
        grant(self.org, self.worker_member, "tasks.review.submit")
        self.client.force_login(self.owner)
        self.assertEqual(self.add(self.task, self.predecessor).status_code, 201)
        self.assertTrue(self.dependency_data(self.task)["blocked"])
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 409)
        self.assertNotIn(
            Task.Status.IN_PROGRESS,
            self.client.get(self.task_url(self.task)).json()["allowed_statuses"],
        )
        with self.assertRaises(IntegrityError) as bypass, transaction.atomic():
            Task.objects.filter(pk=self.task.pk).update(status=Task.Status.IN_PROGRESS)
        self.assertEqual(
            bypass.exception.__cause__.diag.constraint_name,
            "task_strict_dependencies_block_start",
        )

        self.client.force_login(self.worker)
        self.assertEqual(self.submit(self.task).status_code, 409)
        self.assertFalse(self.task.review_submissions.exists())
        self.assertEqual(self.submit(self.predecessor).status_code, 204)

        self.client.force_login(self.owner)
        self.assertEqual(self.approve(self.predecessor).status_code, 204)
        data = self.dependency_data(self.task)
        self.assertFalse(data["blocked"])
        self.assertFalse(data["items"][0]["unresolved"])
        self.assertEqual(data["items"][0]["predecessor"]["status"], Task.Status.COMPLETED)
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 200)

    def test_multiple_predecessors_and_cancelled_task_do_not_unblock_strict_edge(self):
        extra = self.make_task()
        self.client.force_login(self.owner)
        strict = self.add(self.task, self.predecessor)
        self.assertEqual(strict.status_code, 201, strict.content)
        self.assertEqual(self.add(self.task, extra, "informational").status_code, 201)
        self.assertEqual(self.status(self.predecessor, Task.Status.CANCELLED).status_code, 200)
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 409)

        data = self.dependency_data(self.task)
        self.assertEqual(len(data["items"]), 2)
        cancelled = next(item for item in data["items"] if item["kind"] == "strict")
        self.assertEqual(cancelled["predecessor"]["status"], Task.Status.CANCELLED)
        self.assertTrue(cancelled["unresolved"])
        self.assertTrue(data["blocked"])
        self.assertEqual(
            write(
                self.client, "delete",
                self.task_url(self.task, f"dependencies/{strict.json()['id']}/"),
            ).status_code,
            204,
        )
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 200)

    def test_new_strict_dependency_does_not_retroactively_block_work_or_review(self):
        grant(self.org, self.worker_member, "tasks.review.submit")
        self.client.force_login(self.owner)
        self.assertEqual(self.status(self.task, Task.Status.IN_PROGRESS).status_code, 200)
        self.assertEqual(self.add(self.task, self.predecessor).status_code, 201)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)

        self.client.force_login(self.worker)
        self.assertEqual(self.submit(self.task).status_code, 204)
        self.client.force_login(self.owner)
        self.assertEqual(self.approve(self.task).status_code, 204)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.COMPLETED)

    def test_dependency_status_is_visible_without_exposing_hidden_predecessor(self):
        self.client.force_login(self.owner)
        created = self.add(self.task, self.predecessor)
        self.assertEqual(created.status_code, 201, created.content)
        dependency_id = created.json()["id"]
        grant(self.org, self.reader_member, "tasks.view", "tasks.update", department=self.department)
        self.client.force_login(self.reader)

        self.assertEqual(self.client.get(self.task_url(self.predecessor)).status_code, 404)
        response = self.client.get(self.task_url(self.task, "dependencies/"))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["blocked"])
        self.assertEqual(
            data["items"],
            [{"id": dependency_id, "kind": "strict", "predecessor": None, "unresolved": True}],
        )
        self.assertNotContains(response, self.predecessor.name)
        self.assertEqual(self.add(self.task, self.predecessor).status_code, 404)
        self.assertEqual(
            write(self.client, "delete", self.task_url(self.task, f"dependencies/{dependency_id}/"))
            .status_code,
            204,
        )

    def test_links_across_projects_and_departments_within_organization_are_allowed(self):
        first_project = Project.objects.create(
            organization=self.org, name="First project", created_by=self.owner,
        )
        second_project = Project.objects.create(
            organization=self.org, name="Second project", created_by=self.owner,
        )
        first = self.make_task("First project task", project=first_project, department=self.department)
        second = self.make_task(
            "Second project task", project=second_project, department=self.other_department,
        )
        self.client.force_login(self.owner)
        response = self.add(first, second)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(TaskDependency.objects.filter(task=first, predecessor=second).exists())

    def test_api_rejects_cycles_duplicates_foreign_ids_and_missing_csrf(self):
        third = self.make_task()
        self.client.force_login(self.owner)
        self.assertEqual(self.add(self.task, self.predecessor, csrf=False).status_code, 403)
        self.assertEqual(self.add(self.task, self.foreign_task).status_code, 404)
        self.assertEqual(self.add(self.task, self.task).status_code, 409)
        self.assertEqual(self.add(self.task, self.predecessor).status_code, 201)
        self.assertEqual(self.add(self.task, self.predecessor, "informational").status_code, 409)
        self.assertEqual(self.add(self.predecessor, third, "informational").status_code, 201)
        self.assertEqual(self.add(third, self.task).status_code, 409)
        self.assertEqual(TaskDependency.objects.count(), 2)

    def test_read_only_and_cross_organization_users_cannot_mutate_dependencies(self):
        self.client.force_login(self.owner)
        created = self.add(self.task, self.predecessor)
        self.assertEqual(created.status_code, 201, created.content)
        link = self.task_url(self.task, f"dependencies/{created.json()['id']}/")
        grant(self.org, self.reader_member, "tasks.view")
        self.client.force_login(self.reader)
        self.assertFalse(self.dependency_data(self.task)["can_manage"])
        self.assertEqual(self.add(self.predecessor, self.task).status_code, 403)
        self.assertEqual(write(self.client, "delete", link).status_code, 403)

        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.task_url(self.task, "dependencies/")).status_code, 404)
        self.assertEqual(write(self.client, "delete", link).status_code, 404)
        self.assertEqual(TaskDependency.objects.count(), 1)

    def test_reverse_conversion_rejects_both_incoming_and_outgoing_dependencies(self):
        parent = self.make_task("Parent task")
        child_with_predecessor = self.make_task("Outgoing edge", parent=parent)
        child_as_predecessor = self.make_task("Incoming edge", parent=parent)
        checklist = Checklist.objects.create(organization=self.org, task=parent, name="Checklist")
        self.client.force_login(self.owner)
        self.assertEqual(self.add(child_with_predecessor, self.predecessor).status_code, 201)
        self.assertEqual(self.add(self.task, child_as_predecessor).status_code, 201)

        for child in (child_with_predecessor, child_as_predecessor):
            with self.subTest(child=child.pk):
                response = write(
                    self.client, "post", self.task_url(child, "convert-to-item/"),
                    {"checklist_id": checklist.pk, "confirm": True},
                )
                self.assertEqual(response.status_code, 409, response.content)
                child.refresh_from_db()
                self.assertFalse(child.is_archived)
        self.assertFalse(checklist.items.exists())
        self.assertFalse(ChecklistConversion.objects.exists())

    def test_postgresql_rejects_cross_tenant_self_duplicate_and_bulk_cycle(self):
        for first, second in ((self.task, self.foreign_task), (self.task, self.task)):
            with self.subTest(first=first.pk, second=second.pk):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    TaskDependency.objects.create(
                        organization=self.org, task=first, predecessor=second, kind="strict",
                    )
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskDependency.objects.bulk_create([
                TaskDependency(organization=self.org, task=self.task, predecessor=self.predecessor, kind="strict"),
                TaskDependency(organization=self.org, task=self.predecessor, predecessor=self.task, kind="informational"),
            ])
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertFalse(TaskDependency.objects.exists())

        TaskDependency.objects.create(
            organization=self.org, task=self.task, predecessor=self.predecessor, kind="strict",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskDependency.objects.create(
                organization=self.org, task=self.task, predecessor=self.predecessor, kind="informational",
            )


class ConcurrentTaskDependencyTests(TransactionTestCase):
    def test_competing_reciprocal_edges_cannot_commit_a_cycle(self):
        owner = get_user_model().objects.create_superuser(username="dependency-race-owner")
        organization = create_organization(user=owner, name="Dependency race workspace")
        member = organization.memberships.get(user=owner)
        first = Task.objects.create(organization=organization, name="First task", created_by=member)
        second = Task.objects.create(organization=organization, name="Second task", created_by=member)
        start = Barrier(2, timeout=5)

        def connect_tasks(task_id, predecessor_id):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    TaskDependency.objects.create(
                        organization_id=organization.pk, task_id=task_id,
                        predecessor_id=predecessor_id, kind="strict",
                    )
            except IntegrityError as exc:
                return exc.__cause__.diag.constraint_name
            finally:
                worker_connection.close()
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            forward = pool.submit(connect_tasks, first.pk, second.pk)
            reverse = pool.submit(connect_tasks, second.pk, first.pk)
            self.assertCountEqual(
                [forward.result(timeout=12), reverse.result(timeout=12)],
                ["success", "task_dependency_graph_no_cycle"],
            )
        self.assertEqual(TaskDependency.objects.filter(organization=organization).count(), 1)

    def test_strict_link_creation_and_start_share_the_database_serialization_boundary(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="dependency-start-owner")
        worker = users.create_user(username="dependency-start-worker")
        organization = create_organization(user=owner, name="Dependency start race")
        member = organization.memberships.get(user=owner)
        assignee = OrganizationMembership.objects.create(organization=organization, user=worker)
        predecessor = Task.objects.create(
            organization=organization, name="Predecessor", created_by=member,
        )
        task = Task.objects.create(
            organization=organization, name="Concurrent start", created_by=member, assignee=assignee,
        )
        start = Barrier(2, timeout=5)

        def run(operation):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    operation()
            except IntegrityError as exc:
                return exc.__cause__.diag.constraint_name
            finally:
                worker_connection.close()
            return "success"

        def add_edge():
            TaskDependency.objects.create(
                organization=organization, task=task, predecessor=predecessor, kind="strict",
            )

        def begin():
            Task.objects.filter(pk=task.pk).update(status=Task.Status.IN_PROGRESS)

        with ThreadPoolExecutor(max_workers=2) as pool:
            edge = pool.submit(run, add_edge)
            transition = pool.submit(run, begin)
            self.assertEqual(edge.result(timeout=12), "success")
            result = transition.result(timeout=12)
        task.refresh_from_db()
        self.assertIn(
            (result, task.status),
            {
                ("success", Task.Status.IN_PROGRESS),
                ("task_strict_dependencies_block_start", Task.Status.PENDING),
            },
        )
        self.assertTrue(TaskDependency.objects.filter(task=task, predecessor=predecessor).exists())
