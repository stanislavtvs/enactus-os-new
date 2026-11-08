from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task, TaskDistribution, TaskReviewDecision, TaskReviewSubmission
from apps.tasks.reviews import decide_review
from apps.tasks.services import TaskConflict
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="workflow-owner")
        cls.foreign_owner = users.create_superuser(username="workflow-foreign-owner")
        cls.lead = users.create_user(username="workflow-lead")
        cls.allocator = users.create_user(username="workflow-allocator")
        cls.reviewer = users.create_user(username="workflow-department-reviewer")
        cls.worker = users.create_user(username="workflow-worker")
        cls.not_in_project = users.create_user(username="workflow-not-in-project")
        cls.unrelated = users.create_user(username="workflow-unrelated")
        cls.org = create_organization(user=cls.owner, name="Workflow organization")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign workflow organization")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.lead_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.lead)
        cls.allocator_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.allocator
        )
        cls.reviewer_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.reviewer
        )
        cls.worker_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.worker
        )
        cls.not_in_project_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.not_in_project
        )
        cls.unrelated_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.unrelated
        )
        cls.parent = Department.objects.create(organization=cls.org, name="Workflows parent")
        cls.target = Department.objects.create(
            organization=cls.org, parent=cls.parent, name="Workflows target"
        )
        cls.child = Department.objects.create(
            organization=cls.org, parent=cls.target, name="Workflows child"
        )
        cls.orphan = Department.objects.create(organization=cls.org, name="Without allocator")
        cls.project = Project.objects.create(
            organization=cls.org, name="Workflow project", lead_membership=cls.lead_member,
            responsible_department=cls.target, status=Project.Status.ACTIVE, created_by=cls.owner,
        )
        cls.foreign_task = Task.objects.create(
            organization=cls.foreign, name="Foreign task", created_by=cls.foreign_member
        )
        for member in (cls.worker_member, cls.not_in_project_member):
            DepartmentMembership.objects.create(
                organization=cls.org, department=cls.child, organization_membership=member
            )
        ProjectMembership.objects.create(
            organization=cls.org, project=cls.project, organization_membership=cls.worker_member
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.tasks_url = f"{self.org_url}tasks/"

    def task(self, *, project=None, department=None, assignee=None, status=Task.Status.PENDING):
        return Task.objects.create(
            organization=self.org, name="Task with workflow", created_by=self.owner_member,
            project=project, department=department, assignee=assignee, status=status,
        )

    def task_url(self, task, suffix=""):
        return f"{self.tasks_url}{task.pk}/{suffix}"

    def workflow(self, task):
        response = self.client.get(self.task_url(task, "workflow/"))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def route(self, task, *, csrf=True):
        return write(self.client, "post", self.task_url(task, "distribution/"), {}, csrf=csrf)

    def allocate(self, task, member, *, csrf=True):
        return write(
            self.client, "put", self.task_url(task, "distribution/"),
            {"assignee_id": member.pk}, csrf=csrf,
        )

    def set_policy(self, task, policy, reviewer=None):
        response = write(
            self.client, "put", self.task_url(task, "review/policy/"),
            {"policy": policy, "reviewer_id": reviewer.pk if reviewer else None},
        )
        self.assertEqual(response.status_code, 204, response.content)

    def submit(self, task, comment="Ready for review", links=None, *, csrf=True):
        return write(
            self.client, "post", self.task_url(task, "review/submissions/"),
            {"comment": comment, "links": links if links is not None else ["https://example.invalid/result"]},
            csrf=csrf,
        )

    def decide(self, task, stage, outcome="approved", comment="", *, csrf=True):
        return write(
            self.client, "post", self.task_url(task, "review/decisions/"),
            {"stage_id": stage, "outcome": outcome, "comment": comment}, csrf=csrf,
        )

    def grant_allocator(self, *, descendants=True):
        return grant(
            self.org, self.allocator_member, "tasks.distribution.view", "tasks.distribute", "tasks.update",
            department=self.parent, descendants=descendants,
        )

    def grant_department_reviewer(self):
        return grant(
            self.org, self.reviewer_member, "tasks.review.approve", "tasks.review.return", "tasks.update",
            department=self.parent, descendants=True,
        )

    def grant_project_reviewer(self):
        return grant(
            self.org, self.lead_member, "projects.view", "tasks.review.approve", "tasks.review.return",
        )

    def grant_worker(self):
        return grant(self.org, self.worker_member, "tasks.review.submit")

    def queue_ids(self, queue):
        response = self.client.get(self.tasks_url, {"queue": queue})
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()["items"]}

    def test_department_routing_keeps_one_task_and_checks_team_and_candidate_scope(self):
        self.grant_allocator()
        task = self.task(project=self.project, department=self.target)
        grant(self.org, self.lead_member, "projects.view", "tasks.view", "tasks.assign")
        self.client.force_login(self.lead)
        self.assertEqual(
            write(
                self.client, "put", self.task_url(task, "assignee/"),
                {"assignee_id": self.worker_member.pk},
            ).status_code,
            409,
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.route(task, csrf=False).status_code, 403)
        routed = self.route(task)
        self.assertEqual(routed.status_code, 204, routed.content)
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)
        self.assertEqual(self.org.tasks.count(), 1)
        self.assertTrue(self.workflow(task)["distribution"]["pending"])

        self.client.force_login(self.allocator)
        self.assertIn(task.pk, self.queue_ids("distribution"))
        distribution = self.workflow(task)["distribution"]
        self.assertTrue(distribution["can_allocate"])
        self.assertEqual({row["id"] for row in distribution["candidates"]}, {self.worker_member.pk})
        self.assertFalse(self.client.get(self.task_url(task)).json()["permissions"]["update"])
        self.assertEqual(
            self.client.get(f"{self.org_url}projects/{self.project.pk}/").status_code, 404
        )
        self.assertEqual(
            write(self.client, "patch", self.task_url(task), {"name": "Unauthorized rename"})
            .status_code,
            404,
        )
        self.assertEqual(self.allocate(task, self.foreign_member).status_code, 404)
        self.assertEqual(self.allocate(task, self.not_in_project_member).status_code, 409)
        self.assertFalse(
            ProjectMembership.objects.filter(
                project=self.project, organization_membership=self.not_in_project_member
            ).exists()
        )
        self.assertEqual(self.allocate(task, self.worker_member, csrf=False).status_code, 403)
        allocated = self.allocate(task, self.worker_member)
        self.assertEqual(allocated.status_code, 204, allocated.content)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.worker_member.pk)
        self.assertEqual(self.org.tasks.count(), 1)
        self.assertNotIn(task.pk, self.queue_ids("distribution"))
        history = TaskDistribution.objects.filter(task=task).get()
        self.assertEqual(history.requested_by_id, self.owner_member.pk)
        self.assertEqual(history.resolved_by_id, self.allocator_member.pk)
        self.assertEqual(history.assignee_id, self.worker_member.pk)

        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(self.task_url(task)).status_code, 200)

    def test_owner_can_find_unallocated_tasks_without_department_delegate(self):
        task = self.task(department=self.orphan)
        self.client.force_login(self.owner)
        self.assertEqual(self.route(task).status_code, 204)
        self.assertIn(task.pk, self.queue_ids("distribution"))
        response = self.client.get(self.tasks_url, {"distribution_pending": "true"})
        self.assertEqual({row["id"] for row in response.json()["items"]}, {task.pk})

        self.client.force_login(self.unrelated)
        self.assertNotIn(task.pk, self.queue_ids("distribution"))
        self.assertEqual(self.client.get(self.task_url(task, "workflow/")).status_code, 404)
        self.assertEqual(self.allocate(task, self.worker_member).status_code, 404)

    def test_direct_assignment_remains_available_and_records_history(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        task = self.task(project=self.project, department=self.target)
        grant(self.org, self.lead_member, "projects.view", "tasks.view", "tasks.assign")
        self.client.force_login(self.lead)
        response = write(
            self.client, "put", self.task_url(task, "assignee/"),
            {"assignee_id": self.worker_member.pk},
        )
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.worker_member.pk)
        self.assertEqual(TaskDistribution.objects.get(task=task).mode, "direct")

    def test_project_lead_review_completes_only_after_explicit_decision(self):
        self.grant_worker()
        task = self.task(
            project=self.project, department=self.target, assignee=self.worker_member,
            status=Task.Status.IN_PROGRESS,
        )
        self.client.force_login(self.owner)
        self.set_policy(task, "project")
        for status in (Task.Status.REVIEW, Task.Status.COMPLETED):
            with self.subTest(status=status):
                self.assertEqual(
                    write(self.client, "put", self.task_url(task, "status/"), {"status": status}).status_code,
                    409,
                )
        self.assertEqual(self.submit(task).status_code, 403)

        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task, csrf=False).status_code, 403)
        self.assertEqual(self.submit(task, links=["javascript:alert(1)"]).status_code, 422)
        self.assertEqual(self.submit(task).status_code, 409)
        self.assertFalse(task.review_submissions.exists())

        self.grant_project_reviewer()
        response = self.submit(task, "Project result")
        self.assertEqual(response.status_code, 204, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REVIEW)
        self.assertIsNone(task.completed_at)

        self.client.force_login(self.lead)
        self.assertIn(task.pk, self.queue_ids("review"))
        review = self.workflow(task)["review"]
        stage_id = review["current_stage_id"]
        self.assertTrue(review["can_approve"])
        self.assertEqual(self.decide(task, stage_id, csrf=False).status_code, 403)
        response = self.decide(task, stage_id)
        self.assertEqual(response.status_code, 204, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertIsNotNone(task.completed_at)
        self.assertNotIn(task.pk, self.queue_ids("review"))
        self.assertEqual(TaskReviewDecision.objects.get(stage_id=stage_id).actor_id, self.lead_member.pk)

    def test_standalone_department_review_requires_department_reviewer(self):
        self.grant_department_reviewer()
        self.grant_worker()
        task = self.task(
            department=self.target, assignee=self.worker_member, status=Task.Status.IN_PROGRESS,
        )
        self.client.force_login(self.owner)
        self.set_policy(task, "department")
        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task).status_code, 204)

        self.client.force_login(self.reviewer)
        review = self.workflow(task)["review"]
        self.assertTrue(review["can_approve"])
        self.assertEqual(self.decide(task, review["current_stage_id"]).status_code, 204)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_two_stage_return_and_resubmission_reset_all_stages_and_preserve_history(self):
        self.grant_department_reviewer()
        self.grant_project_reviewer()
        self.grant_worker()
        task = self.task(
            project=self.project, department=self.target, assignee=self.worker_member,
            status=Task.Status.IN_PROGRESS,
        )
        self.client.force_login(self.owner)
        self.set_policy(task, "department_project")
        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task, "First version").status_code, 204)

        self.client.force_login(self.reviewer)
        review = self.workflow(task)["review"]
        first_round = review["submissions"][0]
        self.assertFalse(self.client.get(self.task_url(task)).json()["permissions"]["update"])
        self.assertEqual([stage["kind"] for stage in first_round["stages"]], ["department", "project"])
        department_stage, project_stage = [stage["id"] for stage in first_round["stages"]]
        self.assertEqual(review["current_stage_id"], department_stage)
        self.assertEqual(
            self.client.get(f"{self.org_url}projects/{self.project.pk}/").status_code, 404
        )
        self.assertEqual(
            write(self.client, "patch", self.task_url(task), {"description": "Unauthorized"})
            .status_code,
            404,
        )
        self.client.force_login(self.lead)
        self.assertEqual(self.decide(task, project_stage).status_code, 409)
        self.assertFalse(TaskReviewDecision.objects.filter(task=task).exists())
        self.client.force_login(self.reviewer)
        self.assertEqual(self.decide(task, department_stage).status_code, 204)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REVIEW)
        self.assertIsNone(task.completed_at)

        self.client.force_login(self.lead)
        self.assertEqual(self.workflow(task)["review"]["current_stage_id"], project_stage)
        self.assertEqual(self.decide(task, project_stage, "returned", "  ").status_code, 422)
        self.assertEqual(self.decide(task, project_stage, "returned", "Fix the figures").status_code, 204)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)

        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task, "Revised version").status_code, 204)
        self.client.force_login(self.reviewer)
        review = self.workflow(task)["review"]
        rounds = {entry["comment"]: entry for entry in review["submissions"]}
        self.assertEqual(set(rounds), {"First version", "Revised version"})
        next_department, next_project = [stage["id"] for stage in rounds["Revised version"]["stages"]]
        self.assertNotEqual(next_department, department_stage)
        self.assertEqual(review["current_stage_id"], next_department)
        self.assertEqual(self.decide(task, department_stage).status_code, 409)
        self.assertEqual(self.decide(task, next_department).status_code, 204)
        self.client.force_login(self.lead)
        self.assertEqual(self.decide(task, next_project).status_code, 204)
        self.assertEqual(TaskReviewSubmission.objects.filter(task=task).count(), 2)
        self.assertEqual(TaskReviewDecision.objects.get(stage_id=project_stage).comment, "Fix the figures")
        self.assertEqual(TaskReviewDecision.objects.filter(stage_id__in=[
            department_stage, project_stage, next_department, next_project,
        ]).count(), 4)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_submitter_cannot_approve_own_result_even_with_review_grant(self):
        self.grant_worker()
        self.grant_department_reviewer()
        grant(self.org, self.worker_member, "tasks.review.approve", "tasks.review.return")
        task = self.task(
            department=self.target, assignee=self.worker_member, status=Task.Status.IN_PROGRESS,
        )
        self.client.force_login(self.owner)
        self.set_policy(task, "department")
        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task).status_code, 204)
        review = self.workflow(task)["review"]
        self.assertFalse(review["can_approve"])
        self.assertFalse(review["can_return"])
        self.assertEqual(self.decide(task, review["current_stage_id"]).status_code, 403)
        self.assertFalse(TaskReviewDecision.objects.exists())

    def test_review_policy_precedence_and_explicit_standalone_reviewer(self):
        self.grant_worker()
        grant(
            self.org, self.reviewer_member, "tasks.view", "tasks.review.manage",
            "tasks.review.approve", "tasks.review.return",
        )
        task = self.task(project=self.project, department=self.target)
        self.client.force_login(self.owner)
        organization_policy = f"{self.org_url}task-review-policy/"
        project_policy = f"{self.org_url}projects/{self.project.pk}/task-review-policy/"
        response = write(self.client, "put", organization_policy, {"policy": "project"})
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(self.workflow(task)["review"]["policy"], "project")
        response = write(
            self.client, "put", project_policy, {"policy": "department_project"},
        )
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(self.workflow(task)["review"]["policy"], "department_project")
        self.set_policy(task, "department")
        self.assertEqual(self.workflow(task)["review"]["policy"], "department")

        standalone = self.task(assignee=self.worker_member, status=Task.Status.IN_PROGRESS)
        self.client.force_login(self.reviewer)
        response = write(
            self.client, "put", self.task_url(standalone, "review/policy/"),
            {"policy": None, "reviewer_id": self.reviewer_member.pk},
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.client.force_login(self.owner)
        self.set_policy(standalone, None, self.reviewer_member)
        self.client.force_login(self.worker)
        self.assertEqual(self.submit(standalone).status_code, 204)
        self.client.force_login(self.reviewer)
        review = self.workflow(standalone)["review"]
        stages = review["submissions"][0]["stages"]
        self.assertEqual([stage["kind"] for stage in stages], ["explicit"])
        self.assertEqual(self.decide(standalone, stages[0]["id"]).status_code, 204)
        standalone.refresh_from_db()
        self.assertEqual(standalone.status, Task.Status.COMPLETED)

    def test_assignment_or_policy_changes_cannot_create_a_self_review_path(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")
        grant(
            self.org, self.lead_member, "projects.view", "tasks.view", "tasks.assign",
            "tasks.review.approve",
        )
        self.grant_worker()
        grant(self.org, self.worker_member, "tasks.review.approve")
        ProjectMembership.objects.create(
            organization=self.org, project=self.project,
            organization_membership=self.not_in_project_member,
        )
        task = self.task(
            project=self.project, department=self.target, assignee=self.worker_member,
            status=Task.Status.IN_PROGRESS,
        )
        self.client.force_login(self.owner)
        self.set_policy(task, "project")
        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task).status_code, 204)
        stage = self.workflow(task)["review"]["current_stage_id"]

        self.client.force_login(self.lead)
        response = write(
            self.client, "put", self.task_url(task, "assignee/"),
            {"assignee_id": self.not_in_project_member.pk},
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.client.force_login(self.owner)
        response = write(
            self.client, "put", self.task_url(task, "review/policy/"),
            {"policy": "department", "reviewer_id": None},
        )
        self.assertEqual(response.status_code, 409, response.content)

        lead_url = f"{self.org_url}projects/{self.project.pk}/lead/"
        self.assertEqual(
            write(self.client, "put", lead_url, {"lead_membership_id": self.worker_member.pk}).status_code,
            200,
        )
        self.client.force_login(self.lead)
        self.assertEqual(self.decide(task, stage).status_code, 403)
        self.client.force_login(self.worker)
        self.assertEqual(self.decide(task, stage).status_code, 403)
        self.assertFalse(TaskReviewDecision.objects.filter(task=task).exists())
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REVIEW)

    def test_postgresql_rejects_direct_completion_and_cross_tenant_decision(self):
        self.grant_worker()
        task = self.task(
            department=self.target, assignee=self.worker_member, status=Task.Status.IN_PROGRESS,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.filter(pk=task.pk).update(
                status=Task.Status.COMPLETED, completed_at=timezone.now(),
            )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)

        self.client.force_login(self.worker)
        self.assertEqual(self.submit(task).status_code, 204)
        stage = task.review_submissions.get().stages.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskReviewDecision.objects.bulk_create([
                TaskReviewDecision(
                    organization=self.org, task=task, stage=stage, actor=self.foreign_member,
                    outcome="approved",
                )
            ])
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertFalse(TaskReviewDecision.objects.filter(task=task).exists())

    def test_workflow_endpoints_do_not_cross_organizations_or_allow_anonymous_reads(self):
        task = self.task(department=self.target)
        self.assertEqual(self.client.get(self.task_url(task, "workflow/")).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.task_url(task, "workflow/")).status_code, 404)
        self.assertEqual(self.route(task).status_code, 404)
        self.assertEqual(self.submit(task).status_code, 404)
        self.assertEqual(self.decide(task, 1).status_code, 404)

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.task_url(self.foreign_task, "workflow/")).status_code, 404)
        self.assertEqual(self.route(self.foreign_task).status_code, 404)


class ConcurrentTaskReviewTests(TransactionTestCase):
    def test_competing_accept_and_return_create_exactly_one_decision(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="workflow-race-owner")
        worker = users.create_user(username="workflow-race-worker")
        organization = create_organization(user=owner, name="Concurrent task reviews")
        owner_member = organization.memberships.get(user=owner)
        worker_member = OrganizationMembership.objects.create(organization=organization, user=worker)
        grant(organization, worker_member, "tasks.review.submit")
        department = Department.objects.create(organization=organization, name="Review department")
        task = Task.objects.create(
            organization=organization, name="Task reviewed concurrently", department=department,
            created_by=owner_member, assignee=worker_member, status=Task.Status.IN_PROGRESS,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(worker)
        url = f"{API}{organization.pk}/tasks/{task.pk}/review/submissions/"
        response = write(client, "post", url, {"comment": "Ready", "links": []})
        self.assertEqual(response.status_code, 204, response.content)
        stage_id = task.review_submissions.get().stages.get().pk
        start = Barrier(2, timeout=5)

        def decide(outcome, comment):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    decide_review(
                        user=owner, org_id=organization.pk, task_id=task.pk,
                        stage_id=stage_id, outcome=outcome, comment=comment,
                    )
            except TaskConflict:
                return None
            finally:
                worker_connection.close()
            return outcome

        with ThreadPoolExecutor(max_workers=2) as pool:
            approve = pool.submit(decide, "approved", "")
            send_back = pool.submit(decide, "returned", "Needs correction")
            outcomes = [approve.result(timeout=12), send_back.result(timeout=12)]
        self.assertEqual(outcomes.count(None), 1)
        decision = TaskReviewDecision.objects.get(stage_id=stage_id)
        self.assertIn(decision.outcome, outcomes)
        task.refresh_from_db()
        if decision.outcome == "approved":
            self.assertEqual(task.status, Task.Status.COMPLETED)
            self.assertIsNotNone(task.completed_at)
        else:
            self.assertEqual(task.status, Task.Status.IN_PROGRESS)
            self.assertIsNone(task.completed_at)
