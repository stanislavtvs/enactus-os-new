from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from apps.departments.models import Department
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Checklist, ChecklistConversion, ChecklistItem, Task
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskDecompositionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="decomposition-owner")
        cls.foreign_owner = users.create_superuser(username="decomposition-foreign-owner")
        cls.reader = users.create_user(username="decomposition-reader")
        cls.worker = users.create_user(username="decomposition-worker")
        cls.another_worker = users.create_user(username="decomposition-other-worker")
        cls.org = create_organization(user=cls.owner, name="Decomposition workspace")
        cls.foreign = create_organization(
            user=cls.foreign_owner, name="Foreign decomposition workspace",
        )
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_member = cls.foreign.memberships.get(user=cls.foreign_owner)
        cls.reader_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.reader,
        )
        cls.worker_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.worker,
        )
        cls.another_worker_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.another_worker,
        )
        cls.department = Department.objects.create(organization=cls.org, name="Decomposition team")
        cls.project = Project.objects.create(
            organization=cls.org, name="Decomposition project", created_by=cls.owner,
            responsible_department=cls.department, lead_membership=cls.owner_member,
        )
        for member in (cls.worker_member, cls.another_worker_member):
            ProjectMembership.objects.create(
                organization=cls.org, project=cls.project, organization_membership=member,
            )
        cls.parent = Task.objects.create(
            organization=cls.org, name="Root task", created_by=cls.owner_member,
        )
        cls.project_parent = Task.objects.create(
            organization=cls.org, name="Project parent", created_by=cls.owner_member,
            project=cls.project, department=cls.department,
        )
        cls.foreign_task = Task.objects.create(
            organization=cls.foreign, name="Foreign decomposition task", created_by=cls.foreign_member,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.tasks_url = f"{API}{self.org.pk}/tasks/"

    def task_url(self, task, suffix=""):
        return f"{self.tasks_url}{task.pk}/{suffix}"

    def child(self, parent, name="Child task", **fields):
        response = write(
            self.client, "post", self.task_url(parent, "subtasks/"), {"name": name, **fields},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Task.objects.get(pk=response.json()["id"])

    def checklist(self, task=None, name="Before delivery"):
        task = task or self.parent
        response = write(
            self.client, "post", self.task_url(task, "checklists/"), {"name": name},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Checklist.objects.get(pk=response.json()["id"])

    def checklist_url(self, checklist, suffix=""):
        return self.task_url(checklist.task, f"checklists/{checklist.pk}/{suffix}")

    def item(self, checklist, name="Review draft"):
        response = write(
            self.client, "post", self.checklist_url(checklist, "items/"), {"name": name},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return ChecklistItem.objects.get(pk=response.json()["id"])

    def item_url(self, item, suffix=""):
        return self.checklist_url(item.checklist, f"items/{item.pk}/{suffix}")

    def enable_direct_assignment(self):
        Organization.objects.filter(pk=self.org.pk).update(task_assignment_mode="direct")

    def set_assignee(self, task, member):
        return write(
            self.client, "put", self.task_url(task, "assignee/"),
            {"assignee_id": member.pk if member else None},
        )

    def test_subtasks_inherit_project_scope_and_keep_independent_assignees_and_reviews(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        child = self.child(self.project_parent, "First level", priority="high")
        grandchild = self.child(child, "Second level")
        self.assertEqual(child.parent_id, self.project_parent.pk)
        self.assertEqual(child.project_id, self.project.pk)
        self.assertEqual(child.department_id, self.department.pk)
        self.assertEqual(grandchild.parent_id, child.pk)
        self.assertEqual(grandchild.project_id, self.project.pk)
        self.assertEqual(child.priority, Task.Priority.HIGH)

        self.assertEqual(self.set_assignee(self.project_parent, self.worker_member).status_code, 200)
        self.assertEqual(self.set_assignee(child, self.another_worker_member).status_code, 200)
        grant(self.org, self.another_worker_member, "tasks.review.submit")
        self.client.force_login(self.another_worker)
        mine = self.client.get(self.tasks_url, {"mine": "true"}).json()
        self.assertEqual([row["id"] for row in mine["items"]], [child.pk])
        self.assertEqual(
            write(
                self.client, "post", self.task_url(child, "review/submissions/"),
                {"comment": "Child result", "links": []},
            ).status_code,
            204,
        )
        stage = child.review_submissions.get().stages.get()
        self.client.force_login(self.owner)
        self.assertEqual(
            write(
                self.client, "post", self.task_url(child, "review/decisions/"),
                {"stage_id": stage.pk, "outcome": "approved", "comment": ""},
            ).status_code,
            204,
        )
        child.refresh_from_db()
        self.project_parent.refresh_from_db()
        self.assertEqual(child.status, Task.Status.COMPLETED)
        self.assertEqual(child.assignee_id, self.another_worker_member.pk)
        self.assertEqual(self.project_parent.status, Task.Status.PENDING)
        self.assertEqual(self.project_parent.assignee_id, self.worker_member.pk)
        tree = self.client.get(self.task_url(self.project_parent, "subtasks/")).json()
        self.assertEqual(tree["completed"], 1)
        self.assertEqual(tree["total"], 2)
        checklist = self.checklist(self.project_parent)
        rejected = write(
            self.client, "post", self.task_url(child, "convert-to-item/"),
            {"checklist_id": checklist.pk, "confirm": True},
        )
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertEqual(child.review_submissions.get().stages.get().decision.outcome, "approved")

        unfinished = self.child(self.project_parent, "Unfinished direct child")
        detail = self.client.get(self.task_url(self.project_parent)).json()
        self.assertTrue(detail["has_unfinished_children"])
        grant(self.org, self.worker_member, "tasks.review.submit")
        self.client.force_login(self.worker)
        self.assertEqual(
            write(
                self.client, "post", self.task_url(self.project_parent, "review/submissions/"),
                {"comment": "Parent result", "links": []},
            ).status_code,
            204,
        )
        parent_stage = self.project_parent.review_submissions.get().stages.get()
        self.client.force_login(self.owner)
        self.assertEqual(
            write(
                self.client, "post", self.task_url(self.project_parent, "review/decisions/"),
                {"stage_id": parent_stage.pk, "outcome": "approved", "comment": ""},
            ).status_code,
            204,
        )
        self.project_parent.refresh_from_db()
        unfinished.refresh_from_db()
        self.assertEqual(self.project_parent.status, Task.Status.COMPLETED)
        self.assertEqual(unfinished.status, Task.Status.PENDING)

    def test_subtask_tree_paginates_deep_hierarchy_without_query_per_node(self):
        self.client.force_login(self.owner)
        root_child = self.child(self.parent)
        url = self.task_url(self.parent, "subtasks/")
        with CaptureQueriesContext(connection) as baseline:
            response = self.client.get(url, {"page_size": 40})
            self.assertEqual(response.status_code, 200, response.content)

        previous = root_child
        for index in range(15):
            previous = Task.objects.create(
                organization=self.org, name=f"Nested task {index}", created_by=self.owner_member,
                parent=previous,
            )
        with CaptureQueriesContext(connection) as populated:
            response = self.client.get(url, {"page_size": 40})
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 16)
        self.assertEqual(len(response.json()["items"]), 16)
        self.assertIn(previous.pk, {row["id"] for row in response.json()["items"]})
        self.assertLessEqual(len(populated), len(baseline) + 2)
        page = self.client.get(url, {"page": 2, "page_size": 4}).json()
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["page_size"], 4)
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(page["total"], 16)

    def test_parent_move_rejects_self_descendant_and_foreign_parent(self):
        self.client.force_login(self.owner)
        created = write(
            self.client, "post", self.tasks_url, {"name": "Child", "parent_id": self.parent.pk},
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["parent_id"], self.parent.pk)
        child = Task.objects.get(pk=created.json()["id"])
        leaf = self.child(child)
        for target, expected in ((self.parent, 409), (leaf, 409), (self.foreign_task, 404)):
            with self.subTest(target=target.pk):
                response = write(
                    self.client, "put", self.task_url(self.parent, "parent/"),
                    {"parent_id": target.pk},
                )
                self.assertEqual(response.status_code, expected, response.content)
        self.parent.refresh_from_db()
        self.assertIsNone(self.parent.parent_id)
        response = write(
            self.client, "put", self.task_url(child, "parent/"), {"parent_id": None},
        )
        self.assertEqual(response.status_code, 200, response.content)
        child.refresh_from_db()
        leaf.refresh_from_db()
        self.assertIsNone(child.parent_id)
        self.assertEqual(leaf.parent_id, child.pk)

    def test_parent_archive_keeps_subtasks_and_their_data(self):
        self.client.force_login(self.owner)
        child = self.child(self.parent, description="Keep me")
        self.assertEqual(write(self.client, "delete", self.task_url(self.parent)).status_code, 204)
        child.refresh_from_db()
        self.assertEqual(child.parent_id, self.parent.pk)
        self.assertFalse(child.is_archived)
        self.assertEqual(child.description, "Keep me")
        self.assertEqual(self.client.get(self.task_url(child)).status_code, 200)
        response = write(
            self.client, "post", self.task_url(self.parent, "subtasks/"), {"name": "Too late"},
        )
        self.assertEqual(response.status_code, 409, response.content)

    def test_subtask_tree_and_parent_metadata_hide_inaccessible_project_tasks(self):
        Task.objects.create(
            organization=self.org, name="Hidden project child", created_by=self.owner_member,
            parent=self.parent, project=self.project, department=self.department,
        )
        visible = Task.objects.create(
            organization=self.org, name="Visible child of hidden parent",
            created_by=self.owner_member, parent=self.project_parent,
        )
        grant(self.org, self.reader_member, "tasks.view")
        self.client.force_login(self.reader)
        tree = self.client.get(self.task_url(self.parent, "subtasks/"))
        self.assertEqual(tree.status_code, 200, tree.content)
        self.assertEqual(tree.json()["items"], [])
        self.assertEqual(tree.json()["total"], 0)

        detail = self.client.get(self.task_url(visible))
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertIsNone(detail.json()["parent_id"])
        self.assertIsNone(detail.json()["parent"])
        self.assertNotContains(detail, self.project_parent.name)

    def test_multiple_checklists_reorder_and_completion_keep_metadata(self):
        self.client.force_login(self.owner)
        first = self.checklist()
        second = self.checklist(name="After delivery")
        one = self.item(first, "First item")
        two = self.item(first, "Second item")
        self.item(second, "Another list item")
        response = write(self.client, "patch", self.item_url(one), {"is_completed": True})
        self.assertEqual(response.status_code, 200, response.content)
        one.refresh_from_db()
        self.assertTrue(one.is_completed)
        self.assertIsNotNone(one.completed_at)
        self.assertEqual(one.completed_by_id, self.owner_member.pk)

        url = self.checklist_url(first, "order/")
        response = write(self.client, "put", url, {"item_ids": [two.pk, one.pk]})
        self.assertEqual(response.status_code, 204, response.content)
        invalid = write(self.client, "put", url, {"item_ids": [one.pk, one.pk]})
        self.assertEqual(invalid.status_code, 409, invalid.content)
        response = self.client.get(self.task_url(self.parent, "decomposition/"))
        self.assertEqual(response.status_code, 200)
        checklists = {row["id"]: row for row in response.json()["checklists"]}
        self.assertEqual(set(checklists), {first.pk, second.pk})
        self.assertEqual(checklists[first.pk]["completed"], 1)
        self.assertEqual(checklists[first.pk]["total"], 2)
        self.assertEqual([row["id"] for row in checklists[first.pk]["items"]], [two.pk, one.pk])
        self.assertEqual(
            [row["is_completed"] for row in checklists[first.pk]["items"]], [False, True],
        )
        self.assertEqual(Task.objects.filter(organization=self.org).count(), 2)

        response = write(self.client, "patch", self.item_url(one), {"is_completed": False})
        self.assertEqual(response.status_code, 200, response.content)
        one.refresh_from_db()
        self.assertFalse(one.is_completed)
        self.assertIsNone(one.completed_by_id)
        self.assertIsNone(one.completed_at)

    def test_completed_item_converts_to_pending_subtask_and_history_is_preserved(self):
        self.client.force_login(self.owner)
        checklist = self.checklist()
        source = self.item(checklist, "Completed checklist source")
        self.assertEqual(
            write(self.client, "patch", self.item_url(source), {"is_completed": True}).status_code,
            200,
        )
        url = self.item_url(source, "convert/")
        self.assertEqual(write(self.client, "post", url, {"confirm": False}).status_code, 422)
        response = write(self.client, "post", url, {"confirm": True})
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(task.parent_id, self.parent.pk)
        self.assertEqual(task.name, source.name)
        self.assertEqual(task.status, Task.Status.PENDING)
        self.assertIsNone(task.completed_at)
        self.assertFalse(task.review_submissions.exists())
        source.refresh_from_db()
        self.assertTrue(source.is_completed)
        self.assertIsNotNone(source.archived_at)
        history = ChecklistConversion.objects.get()
        self.assertEqual(history.direction, "item_to_task")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChecklistConversion.objects.filter(pk=history.pk).update(actor=self.foreign_member)
        repeated = write(self.client, "post", url, {"confirm": True})
        self.assertEqual(repeated.status_code, 404, repeated.content)
        self.assertEqual(self.parent.subtasks.count(), 1)

    def test_conversion_rolls_back_new_task_if_history_write_fails(self):
        self.client.force_login(self.owner)
        source = self.item(self.checklist())
        url = self.item_url(source, "convert/")
        with patch.object(ChecklistConversion, "save", side_effect=RuntimeError("history failure")):
            with self.assertRaisesMessage(RuntimeError, "history failure"):
                write(self.client, "post", url, {"confirm": True})
        source.refresh_from_db()
        self.assertIsNone(source.archived_at)
        self.assertFalse(self.parent.subtasks.exists())
        self.assertFalse(ChecklistConversion.objects.exists())

    def test_conversion_history_redacts_task_identity_without_losing_the_source_item(self):
        parent = Task.objects.create(
            organization=self.org, name="Reader's task", created_by=self.reader_member,
        )
        self.client.force_login(self.owner)
        source = self.item(self.checklist(parent), "Original visible item")
        response = write(self.client, "post", self.item_url(source, "convert/"), {"confirm": True})
        self.assertEqual(response.status_code, 201, response.content)
        child = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(
            write(self.client, "patch", self.task_url(child), {"name": "Private child title"})
            .status_code,
            200,
        )

        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.task_url(child)).status_code, 404)
        response = self.client.get(self.task_url(parent, "decomposition/"))
        self.assertEqual(response.status_code, 200, response.content)
        [conversion] = response.json()["conversions"]
        self.assertEqual(conversion["item_name"], "Original visible item")
        self.assertIsNone(conversion["task_id"])
        self.assertIsNone(conversion["task_name"])
        self.assertNotContains(response, "Private child title")

    def test_safe_reverse_conversion_archives_task_and_retains_source_and_history(self):
        self.client.force_login(self.owner)
        child = self.child(self.parent, "Safe task")
        checklist = self.checklist()
        url = self.task_url(child, "convert-to-item/")
        denied = write(self.client, "post", url, {"checklist_id": checklist.pk, "confirm": False})
        self.assertEqual(denied.status_code, 422, denied.content)
        response = write(
            self.client, "post", url, {"checklist_id": checklist.pk, "confirm": True},
        )
        self.assertEqual(response.status_code, 201, response.content)
        item = ChecklistItem.objects.get(pk=response.json()["id"])
        child.refresh_from_db()
        self.assertEqual(item.checklist_id, checklist.pk)
        self.assertEqual(item.name, child.name)
        self.assertFalse(item.is_completed)
        self.assertTrue(child.is_archived)
        self.assertEqual(child.parent_id, self.parent.pk)
        self.assertEqual(ChecklistConversion.objects.get().direction, "task_to_item")

    def test_reverse_conversion_rejects_children_and_assignment_history_without_changes(self):
        self.enable_direct_assignment()
        self.client.force_login(self.owner)
        checklist = self.checklist()
        assigned = self.child(self.parent, "Previously assigned")
        self.assertEqual(self.set_assignee(assigned, self.worker_member).status_code, 200)
        self.assertEqual(self.set_assignee(assigned, None).status_code, 200)
        nested = self.child(self.parent, "With descendants")
        self.child(nested)
        for child in (assigned, nested):
            with self.subTest(child=child.pk):
                response = write(
                    self.client, "post", self.task_url(child, "convert-to-item/"),
                    {"checklist_id": checklist.pk, "confirm": True},
                )
                self.assertEqual(response.status_code, 409, response.content)
                child.refresh_from_db()
                self.assertFalse(child.is_archived)
        self.assertFalse(ChecklistConversion.objects.exists())
        self.assertFalse(ChecklistItem.objects.filter(checklist=checklist).exists())

    def test_decomposition_keeps_csrf_rbac_and_tenant_boundary(self):
        self.client.force_login(self.owner)
        checklist = self.checklist()
        item = self.item(checklist)
        self.assertEqual(
            write(
                self.client, "post", self.item_url(item, "convert/"), {"confirm": True}, csrf=False,
            ).status_code,
            403,
        )
        grant(self.org, self.reader_member, "tasks.view")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.task_url(self.parent, "decomposition/")).status_code, 200)
        self.assertEqual(
            write(
                self.client, "patch", self.item_url(item), {"is_completed": True},
            ).status_code,
            403,
        )
        self.assertEqual(
            write(
                self.client, "post", self.item_url(item, "convert/"), {"confirm": True},
            ).status_code,
            403,
        )
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.task_url(self.parent, "subtasks/")).status_code, 404)
        self.assertEqual(self.client.get(self.task_url(self.parent, "decomposition/")).status_code, 404)
        self.assertEqual(
            write(
                self.client, "post", self.task_url(self.parent, "checklists/"), {"name": "Foreign"},
            ).status_code,
            404,
        )

    def test_postgresql_rejects_cross_tenant_parent_and_multirow_cycle(self):
        sibling = Task.objects.create(organization=self.org, name="Sibling", created_by=self.owner_member)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.filter(pk=self.parent.pk).update(parent=self.foreign_task)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.parent.parent = sibling
        sibling.parent = self.parent
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.bulk_update([self.parent, sibling], ["parent"])
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.parent.refresh_from_db()
        sibling.refresh_from_db()
        self.assertIsNone(self.parent.parent_id)
        self.assertIsNone(sibling.parent_id)
