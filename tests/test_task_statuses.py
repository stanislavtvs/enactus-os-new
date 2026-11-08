from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase

from apps.departments.models import Department
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.tasks.models import Task, TaskCollaborator, TaskStatus, TaskStatusTransition
from apps.tasks.services import TaskConflict, set_workflow_status
from apps.tasks.statuses import StatusConflict
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="task-status-owner")
        cls.foreign_owner = users.create_superuser(username="task-status-foreign-owner")
        cls.worker = users.create_user(username="task-status-worker")
        cls.collaborator = users.create_user(username="task-status-collaborator")
        cls.manager = users.create_user(username="task-status-manager")
        cls.org = create_organization(user=cls.owner, name="Status workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign status workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.worker_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.worker,
        )
        cls.collaborator_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.collaborator,
        )
        cls.manager_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.manager,
        )
        cls.department = Department.objects.create(organization=cls.org, name="Review department")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.statuses_url = f"{self.org_url}task-statuses/"
        self.transitions_url = f"{self.org_url}task-transitions/"
        self.tasks_url = f"{self.org_url}tasks/"

    def status_url(self, status):
        return f"{self.statuses_url}{status['id']}/"

    def task_url(self, task, suffix=""):
        return f"{self.tasks_url}{task.pk}/{suffix}"

    def make_task(self, name="Configurable task", **fields):
        task = Task.objects.create(
            organization=self.org, name=name, created_by=self.owner_member, **fields,
        )
        task.refresh_from_db()
        return task

    def status_catalog(self):
        response = self.client.get(self.statuses_url)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["code"]: row for row in response.json()["items"]}

    def create_status(self, code, stage="pending", **fields):
        response = write(
            self.client, "post", self.statuses_url,
            {"code": code, "name": code.title(), "stage": stage, "color": "#123456", **fields},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def rule(self, source, target, **flags):
        return write(
            self.client, "post", self.transitions_url,
            {"from_status_id": source["id"], "to_status_id": target["id"], **flags},
        )

    def transition(self, task, target, *, source_id=None, csrf=True):
        return write(
            self.client, "put", self.task_url(task, "workflow-status/"),
            {"status_id": target["id"], "expected_status_id": source_id or task.workflow_status_id},
            csrf=csrf,
        )

    def legacy(self, task, stage):
        return write(
            self.client, "put", self.task_url(task, "status/"), {"status": stage},
        )

    def test_catalog_seeds_each_stage_and_custom_status_rename_keeps_identity(self):
        self.client.force_login(self.owner)
        seeded = self.status_catalog()
        self.assertEqual(set(seeded), set(Task.Status.values))
        for stage, row in seeded.items():
            with self.subTest(stage=stage):
                self.assertEqual(row["stage"], stage)
                self.assertTrue(row["is_default"])
                self.assertTrue(row["is_active"])
        first = self.create_status("ready", name="Ready")
        second = self.create_status("next", name="Next")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["stage"], second["stage"])

        updated = write(
            self.client, "patch", self.status_url(first),
            {"name": "Queued", "color": "#334455", "sort_order": 2},
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.json()["code"], "ready")
        self.assertEqual(updated.json()["stage"], "pending")
        self.assertEqual(updated.json()["name"], "Queued")
        for payload in ({"code": "altered"}, {"stage": "completed"}, {"color": "red; display:none"}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    write(self.client, "patch", self.status_url(first), payload).status_code,
                    422,
                )
        duplicate = write(
            self.client, "post", self.statuses_url,
            {"code": "ready", "name": "Duplicate", "stage": "pending", "color": "#112233"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.content)

    def test_replacing_stage_default_applies_to_new_tasks_without_rewriting_existing_ones(self):
        self.client.force_login(self.owner)
        seeds = self.status_catalog()
        previous = self.make_task("Existing task")
        replacement = self.create_status("pending_default")
        response = write(
            self.client, "patch", self.status_url(replacement), {"is_default": True},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self.status_catalog()["pending"]["is_default"])
        self.assertTrue(self.status_catalog()["in_progress"]["is_default"])
        self.assertEqual(previous.workflow_status_id, seeds["pending"]["id"])

        created = write(self.client, "post", self.tasks_url, {"name": "New default task"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["workflow_status"]["id"], replacement["id"])
        with self.assertRaises(IntegrityError) as missing, transaction.atomic():
            TaskStatus.objects.filter(pk=replacement["id"]).update(is_default=False)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(
            missing.exception.__cause__.diag.constraint_name,
            "task_workflow_stage_default_required",
        )

    def test_archived_status_preserves_existing_task_and_can_be_left_but_not_entered(self):
        self.client.force_login(self.owner)
        pending = self.status_catalog()["pending"]
        old = self.create_status("obsolete")
        self.assertEqual(self.rule(pending, old).status_code, 201)
        self.assertEqual(self.rule(old, pending).status_code, 201)
        task = self.make_task()
        self.assertEqual(self.transition(task, old).status_code, 200)
        task.refresh_from_db()
        self.assertEqual(write(self.client, "delete", self.status_url(pending)).status_code, 409)
        self.assertEqual(write(self.client, "delete", self.status_url(old)).status_code, 204)
        self.assertFalse(self.status_catalog()["obsolete"]["is_active"])
        self.assertEqual(self.client.get(self.task_url(task)).json()["workflow_status"]["id"], old["id"])

        another = self.make_task("New task")
        self.assertEqual(self.transition(another, old).status_code, 409)
        response = self.transition(task, pending)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["workflow_status"]["id"], pending["id"])
        self.assertEqual(response.json()["status"], Task.Status.PENDING)

    def test_legacy_status_route_respects_configured_graph(self):
        self.client.force_login(self.owner)
        stages = self.status_catalog()
        listing = self.client.get(self.transitions_url)
        self.assertEqual(listing.status_code, 200, listing.content)
        start_rule = next(
            row for row in listing.json()["items"]
            if row["from_status_id"] == stages["pending"]["id"]
            and row["to_status_id"] == stages["in_progress"]["id"]
        )
        url = f"{self.transitions_url}{start_rule['id']}/"
        task = self.make_task(assignee=self.worker_member)
        self.assertEqual(write(self.client, "patch", url, {"is_enabled": False}).status_code, 200)
        self.assertEqual(self.legacy(task, Task.Status.IN_PROGRESS).status_code, 409)
        self.assertEqual(self.transition(task, stages["in_progress"]).status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PENDING)

        self.assertEqual(write(self.client, "patch", url, {"is_enabled": True}).status_code, 200)
        response = self.legacy(task, Task.Status.IN_PROGRESS)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["workflow_status"]["id"], stages["in_progress"]["id"])
        self.assertEqual(response.json()["status"], Task.Status.IN_PROGRESS)

    def test_manual_transitions_cannot_bypass_review_and_strict_dependencies(self):
        self.client.force_login(self.owner)
        stages = self.status_catalog()
        task = self.make_task(assignee=self.worker_member)
        for stage in (Task.Status.REVIEW, Task.Status.COMPLETED):
            with self.subTest(stage=stage):
                response = self.rule(stages["pending"], stages[stage])
                self.assertIn(response.status_code, {409, 422}, response.content)
                self.assertEqual(self.transition(task, stages[stage]).status_code, 409)
                self.assertEqual(self.legacy(task, stage).status_code, 409)
        predecessor = self.make_task("Unresolved predecessor")
        response = write(
            self.client, "post", self.task_url(task, "dependencies/"),
            {"predecessor_id": predecessor.pk, "kind": "strict"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.transition(task, stages["in_progress"]).status_code, 409)
        self.assertEqual(self.legacy(task, Task.Status.IN_PROGRESS).status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PENDING)
        self.assertIsNone(task.completed_at)

    def test_assignee_transition_requires_execute_permission_and_every_rule_grant(self):
        self.client.force_login(self.owner)
        pending = self.status_catalog()["pending"]
        target = self.create_status("working_queue")
        response = self.rule(
            pending, target, allow_assignee=True, required_permissions=["tasks.review.submit"],
        )
        self.assertEqual(response.status_code, 201, response.content)
        task = self.make_task(assignee=self.worker_member)
        TaskCollaborator.objects.create(
            organization=self.org, task=task, organization_membership=self.collaborator_member,
        )
        self.client.force_login(self.worker)
        self.assertEqual(self.transition(task, target).status_code, 403)
        grant(self.org, self.worker_member, "tasks.status.execute")
        self.assertEqual(self.transition(task, target).status_code, 403)

        grant(
            self.org, self.collaborator_member, "tasks.status.execute", "tasks.review.submit",
        )
        self.client.force_login(self.collaborator)
        self.assertEqual(self.transition(task, target).status_code, 403)
        grant(self.org, self.manager_member, "tasks.view", "tasks.change_status")
        self.client.force_login(self.manager)
        self.assertEqual(self.transition(task, target).status_code, 403)

        grant(self.org, self.worker_member, "tasks.review.submit")
        self.client.force_login(self.worker)
        response = self.transition(task, target)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["workflow_status"]["id"], target["id"])

    def test_configuration_permissions_csrf_and_runtime_transition_are_separate(self):
        task = self.make_task()
        self.client.force_login(self.manager)
        grant(self.org, self.manager_member, "tasks.view", "tasks.transitions.manage")
        self.assertEqual(self.client.get(self.transitions_url).status_code, 200)
        response = write(
            self.client, "post", self.statuses_url,
            {"code": "denied", "name": "Denied", "stage": "pending", "color": "#123456"},
        )
        self.assertEqual(response.status_code, 403, response.content)

        self.client.force_login(self.owner)
        stages = self.status_catalog()
        target = self.create_status("staged")
        self.client.force_login(self.manager)
        payload = {"from_status_id": stages["pending"]["id"], "to_status_id": target["id"]}
        self.assertEqual(
            write(self.client, "post", self.transitions_url, payload, csrf=False).status_code, 403,
        )
        for grants in (["unknown.permission"], ["tasks.view", "tasks.view"]):
            with self.subTest(grants=grants):
                response = self.rule(stages["pending"], target, required_permissions=grants)
                self.assertEqual(response.status_code, 422, response.content)
        response = self.rule(stages["pending"], target)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.transition(task, target).status_code, 403)

        self.client.force_login(self.owner)
        self.assertEqual(self.transition(task, target, csrf=False).status_code, 403)
        self.assertEqual(self.transition(task, target).status_code, 200)
        self.assertEqual(self.transition(task, target).status_code, 409)

    def test_stage_defaults_apply_to_review_completion_and_membership_deactivation(self):
        self.client.force_login(self.owner)
        pending = self.create_status("waiting_custom", "pending", is_default=True)
        review = self.create_status("approval_custom", "review", is_default=True)
        completed = self.create_status("done_custom", "completed", is_default=True)
        grant(self.org, self.worker_member, "tasks.review.submit")
        task = self.make_task(
            assignee=self.worker_member, department=self.department, status=Task.Status.IN_PROGRESS,
        )
        other = self.make_task(
            "Working task", assignee=self.worker_member, status=Task.Status.IN_PROGRESS,
        )

        self.client.force_login(self.worker)
        response = write(
            self.client, "post", self.task_url(task, "review/submissions/"),
            {"comment": "Ready", "links": []},
        )
        self.assertEqual(response.status_code, 204, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REVIEW)
        self.assertEqual(task.workflow_status_id, review["id"])

        self.client.force_login(self.owner)
        stage = task.review_submissions.get().stages.get()
        response = write(
            self.client, "post", self.task_url(task, "review/decisions/"),
            {"stage_id": stage.pk, "outcome": "approved", "comment": ""},
        )
        self.assertEqual(response.status_code, 204, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(task.workflow_status_id, completed["id"])
        self.assertIsNotNone(task.completed_at)

        OrganizationMembership.objects.filter(pk=self.worker_member.pk).update(is_active=False)
        other.refresh_from_db()
        self.assertEqual(other.status, Task.Status.PENDING)
        self.assertEqual(other.workflow_status_id, pending["id"])
        self.assertIsNone(other.assignee_id)
        task.refresh_from_db()
        self.assertEqual(task.workflow_status_id, completed["id"])

    def test_subtask_keeps_its_workflow_status_independent_of_parent(self):
        self.client.force_login(self.owner)
        parent = self.make_task("Parent")
        pending = self.status_catalog()["pending"]
        subtask_status = self.create_status("child_wait")
        self.assertEqual(self.rule(pending, subtask_status).status_code, 201)
        response = write(
            self.client, "post", self.task_url(parent, "subtasks/"), {"name": "Child"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        child = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(self.transition(child, subtask_status).status_code, 200)
        child.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(child.workflow_status_id, subtask_status["id"])
        self.assertEqual(parent.workflow_status_id, pending["id"])
        self.assertEqual(child.parent_id, parent.pk)
        response = self.client.get(self.tasks_url, {"workflow_status_id": subtask_status["id"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["id"] for row in response.json()["items"]], [child.pk])

    def test_foreign_status_and_transition_endpoints_and_postgresql_scope_are_protected(self):
        task = self.make_task()
        foreign = TaskStatus.objects.get(organization=self.foreign, code="pending")
        self.assertEqual(self.client.get(self.statuses_url).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.statuses_url).status_code, 404)
        self.assertEqual(self.client.get(self.transitions_url).status_code, 404)
        self.client.force_login(self.owner)
        pending = self.status_catalog()["pending"]
        self.assertEqual(self.rule(pending, {"id": foreign.pk}).status_code, 404)
        self.assertEqual(self.transition(task, {"id": foreign.pk}).status_code, 404)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.filter(pk=task.pk).update(workflow_status=foreign)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        wrong_stage = self.org.task_statuses.get(code="in_progress")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.filter(pk=task.pk).update(workflow_status=wrong_stage)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        task.refresh_from_db()
        self.assertEqual(task.workflow_status_id, pending["id"])

        local = self.org.task_statuses.get(code="pending")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskStatusTransition.objects.create(
                organization=self.org, from_status=local, to_status=foreign,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class ConcurrentTaskStatusTests(TransactionTestCase):
    def test_concurrent_commands_cannot_both_use_the_same_expected_status(self):
        owner = get_user_model().objects.create_superuser(username="task-status-race-owner")
        organization = create_organization(user=owner, name="Status transition race")
        membership = organization.memberships.get(user=owner)
        task = Task.objects.create(
            organization=organization, name="Concurrent workflow change", created_by=membership,
        )
        task.refresh_from_db()
        start_status_id = task.workflow_status_id
        target_ids = []
        for label in ("first", "second"):
            status = TaskStatus.objects.create(
                organization=organization, code=f"concurrent_{label}", name=label,
                stage=Task.Status.PENDING, color="#112233",
            )
            TaskStatusTransition.objects.create(
                organization=organization, from_status_id=start_status_id, to_status=status,
            )
            target_ids.append(status.pk)
        start = Barrier(2, timeout=5)

        def move(target_id):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    set_workflow_status(
                        user=owner, org_id=organization.pk, task_id=task.pk, status_id=target_id,
                        expected_status_id=start_status_id,
                    )
            except (TaskConflict, StatusConflict):
                return None
            finally:
                worker_connection.close()
            return target_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(move, target_id) for target_id in target_ids]
            outcomes = [future.result(timeout=12) for future in futures]
        self.assertEqual(outcomes.count(None), 1)
        task.refresh_from_db()
        self.assertEqual(task.workflow_status_id, next(outcome for outcome in outcomes if outcome))
        self.assertEqual(task.status, Task.Status.PENDING)
