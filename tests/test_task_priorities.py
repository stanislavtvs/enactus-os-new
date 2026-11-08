from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase
from django.urls import reverse

from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.tasks.models import Task, TaskPriority
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"


class TaskPriorityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="priority-owner")
        cls.foreign_owner = users.create_superuser(username="priority-foreign-owner")
        cls.reader = users.create_user(username="priority-reader")
        cls.manager = users.create_user(username="priority-manager")
        cls.org = create_organization(user=cls.owner, name="Priority workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign priority workspace")
        cls.reader_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.reader,
        )
        cls.manager_member = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.manager,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.catalog = f"{self.org_url}task-priorities/"
        self.tasks = f"{self.org_url}tasks/"

    def create_priority(self, code, **fields):
        response = write(
            self.client, "post", self.catalog,
            {"code": code, "name": code.title(), "rank": 400, "color": "#AABBCC", **fields},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def create_task(self, name, **fields):
        response = write(self.client, "post", self.tasks, {"name": name, **fields})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def priority_url(self, priority):
        return f"{self.catalog}{priority['id']}/"

    def task_url(self, task):
        return f"{self.tasks}{task['id']}/"

    def seeded(self):
        response = self.client.get(self.catalog)
        self.assertEqual(response.status_code, 200, response.content)
        return {item["code"]: item for item in response.json()["items"]}

    def test_new_organization_seeds_relative_order_and_a_single_default(self):
        self.client.force_login(self.owner)
        items = self.seeded()
        self.assertEqual(set(items), {"low", "normal", "high"})
        self.assertLess(items["low"]["rank"], items["normal"]["rank"])
        self.assertLess(items["normal"]["rank"], items["high"]["rank"])
        self.assertEqual([code for code, item in items.items() if item["is_default"]], ["normal"])
        self.assertTrue(all(item["is_active"] for item in items.values()))

        foreign_ids = set(TaskPriority.objects.filter(organization=self.foreign).values_list("id", flat=True))
        self.assertTrue(foreign_ids)
        self.assertTrue(foreign_ids.isdisjoint(item["id"] for item in items.values()))

    def test_priorities_can_be_renamed_and_reordered_without_changing_their_codes(self):
        self.client.force_login(self.owner)
        priority = self.create_priority("critical", name="Critical", sort_order=5)
        response = write(
            self.client, "patch", self.priority_url(priority),
            {"name": "Immediate", "rank": 550, "color": "#314159", "sort_order": 1},
        )
        self.assertEqual(response.status_code, 200, response.content)
        updated = response.json()
        self.assertEqual(updated["code"], "critical")
        self.assertEqual(updated["name"], "Immediate")
        self.assertEqual(updated["rank"], 550)
        self.assertEqual(updated["sort_order"], 1)
        self.assertEqual(updated["color"].lower(), "#314159")
        response = write(self.client, "patch", self.priority_url(priority), {"code": "renamed"})
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(self.seeded()["critical"]["name"], "Immediate")

    def test_custom_default_and_legacy_priority_codes_are_compatible(self):
        self.client.force_login(self.owner)
        initial = self.seeded()
        old_task = self.create_task("Legacy high task", priority="high")
        self.assertEqual(old_task["priority"], "high")
        self.assertEqual(old_task["priority_level"]["id"], initial["high"]["id"])

        custom = self.create_priority("urgent", is_default=True)
        rows = self.seeded()
        self.assertEqual([code for code, item in rows.items() if item["is_default"]], ["urgent"])
        default_task = self.create_task("Uses organization default")
        self.assertEqual(default_task["priority"], "normal")
        self.assertEqual(default_task["priority_level"]["id"], custom["id"])

        explicit_normal = self.create_task("Uses canonical normal", priority="normal")
        self.assertEqual(explicit_normal["priority_level"]["id"], initial["normal"]["id"])
        reread = self.client.get(self.task_url(old_task)).json()
        self.assertEqual(reread["priority_level"]["id"], initial["high"]["id"])

        ambiguous = write(
            self.client, "post", self.tasks,
            {"name": "Conflicting priority", "priority": "low", "priority_id": custom["id"]},
        )
        self.assertEqual(ambiguous.status_code, 422, ambiguous.content)
        replacement = write(
            self.client, "patch", self.priority_url(initial["low"]), {"is_default": True},
        )
        self.assertEqual(replacement.status_code, 200, replacement.content)
        self.assertEqual(
            [code for code, item in self.seeded().items() if item["is_default"]], ["low"],
        )
        self.assertEqual(self.create_task("Uses replacement default")["priority"], "low")
        self.assertEqual(
            self.client.get(self.task_url(default_task)).json()["priority_level"]["id"], custom["id"],
        )

    def test_rank_sorting_and_filtering_do_not_depend_on_deadline(self):
        self.client.force_login(self.owner)
        items = self.seeded()
        custom = self.create_priority("critical", rank=items["high"]["rank"] + 100)
        urgent = self.create_task("High rank without deadline", priority_id=custom["id"])
        low = self.create_task(
            "Low rank with deadline", priority="low", due_at="2026-11-09T12:00:00+05:00",
        )
        high = self.create_task("Legacy high", priority="high")
        response = self.client.get(self.tasks, {"sort": "priority"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([task["id"] for task in response.json()["items"]], [urgent["id"], high["id"], low["id"]])
        filtered = self.client.get(self.tasks, {"priority_id": custom["id"], "sort": "priority"})
        self.assertEqual(filtered.status_code, 200, filtered.content)
        self.assertEqual([task["id"] for task in filtered.json()["items"]], [urgent["id"]])

        response = write(
            self.client, "patch", self.priority_url(custom), {"rank": items["low"]["rank"] - 1},
        )
        self.assertEqual(response.status_code, 200, response.content)
        sorted_rows = self.client.get(self.tasks, {"sort": "priority"}).json()["items"]
        self.assertEqual([task["id"] for task in sorted_rows], [high["id"], low["id"], urgent["id"]])
        self.assertIsNone(sorted_rows[-1]["due_at"])

        changed = write(
            self.client, "patch", self.task_url(low), {"priority_id": custom["id"]},
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.json()["priority_level"]["id"], custom["id"])
        self.assertEqual(changed.json()["due_at"], low["due_at"])

    def test_archiving_preserves_catalog_and_rejects_used_default_or_builtin_priorities(self):
        self.client.force_login(self.owner)
        used = self.create_priority("used")
        task = self.create_task("Used priority", priority_id=used["id"])
        default = self.create_priority("new_default", is_default=True)
        unused = self.create_priority("unused")
        builtin = self.seeded()["high"]
        for protected in (used, default, builtin):
            with self.subTest(code=protected["code"]):
                response = write(self.client, "delete", self.priority_url(protected))
                self.assertEqual(response.status_code, 409, response.content)

        archived = write(self.client, "delete", self.priority_url(unused))
        self.assertEqual(archived.status_code, 204, archived.content)
        self.assertFalse(self.seeded()["unused"]["is_active"])
        options = self.client.get(f"{self.org_url}task-options/").json()
        self.assertNotIn(unused["id"], {item["id"] for item in options["priorities"]})
        response = write(
            self.client, "patch", self.task_url(task), {"priority_id": unused["id"]},
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self.client.get(self.task_url(task)).json()["priority_level"]["id"], used["id"])

    def test_workflow_view_configuration_management_and_task_update_are_separate(self):
        self.client.force_login(self.owner)
        task = self.create_task("Priority permission task")
        target = self.seeded()["high"]
        self.client.force_login(self.reader)
        grant(self.org, self.reader_member, "tasks.view")
        self.assertEqual(self.client.get(self.catalog).status_code, 403)
        self.assertEqual(self.client.get(f"{self.org_url}task-options/").status_code, 200)

        grant(self.org, self.reader_member, "tasks.workflow.view")
        catalog = self.client.get(self.catalog)
        self.assertEqual(catalog.status_code, 200)
        self.assertFalse(catalog.json()["can_manage"])
        payload = {"code": "no_bypass", "name": "No bypass", "rank": 100, "color": "#112233"}
        self.assertEqual(write(self.client, "post", self.catalog, payload).status_code, 403)

        self.client.force_login(self.manager)
        grant(self.org, self.manager_member, "tasks.view", "tasks.priorities.manage")
        self.assertTrue(self.client.get(self.catalog).json()["can_manage"])
        self.assertEqual(write(self.client, "post", self.catalog, payload, csrf=False).status_code, 403)
        response = write(self.client, "post", self.catalog, payload)
        self.assertEqual(response.status_code, 201, response.content)
        denied = write(self.client, "patch", self.task_url(task), {"priority_id": target["id"]})
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertEqual(self.client.get(self.task_url(task)).json()["priority_level"]["id"],
                         task["priority_level"]["id"])

    def test_catalog_and_task_assignment_are_tenant_scoped(self):
        foreign = TaskPriority.objects.get(organization=self.foreign, code="high")
        self.assertEqual(self.client.get(self.catalog).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.catalog).status_code, 404)

        self.client.force_login(self.owner)
        foreign_url = f"{self.catalog}{foreign.pk}/"
        self.assertEqual(write(self.client, "patch", foreign_url, {"name": "Wrong org"}).status_code, 404)
        self.assertEqual(write(self.client, "delete", foreign_url).status_code, 404)
        wrong = write(
            self.client, "post", self.tasks, {"name": "Wrong organization", "priority_id": foreign.pk},
        )
        self.assertEqual(wrong.status_code, 404, wrong.content)
        self.assertFalse(Task.objects.filter(organization=self.org, name="Wrong organization").exists())

        task = self.create_task("Valid task")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.filter(pk=task["id"]).update(priority_level=foreign)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(self.client.get(self.task_url(task)).json()["priority_level"]["id"],
                         task["priority_level"]["id"])

    def test_codes_are_unique_and_css_colors_are_validated(self):
        self.client.force_login(self.owner)
        priority = self.create_priority("critical")
        duplicate = write(
            self.client, "post", self.catalog,
            {"code": priority["code"], "name": "Duplicate", "rank": 10, "color": "#112233"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        invalid_color = write(
            self.client, "patch", self.priority_url(priority),
            {"color": "red; background: url(javascript:alert(1))"},
        )
        self.assertEqual(invalid_color.status_code, 422, invalid_color.content)
        self.assertEqual(self.seeded()["critical"]["color"], priority["color"])

    def test_postgresql_protects_default_and_resolves_legacy_codes(self):
        levels = self.org.task_priorities
        normal = levels.get(code="normal")
        high = levels.get(code="high")
        with self.assertRaises(IntegrityError), transaction.atomic():
            levels.filter(pk=high.pk).update(is_default=True)
        with self.assertRaises(IntegrityError) as missing, transaction.atomic():
            levels.filter(pk=normal.pk).update(is_default=False)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(
            missing.exception.__cause__.diag.constraint_name,
            "task_priority_active_default_required",
        )
        self.assertEqual(levels.get(is_default=True), normal)

        task = Task.objects.create(
            organization=self.org,
            created_by=self.org.memberships.get(user=self.owner),
            name="Legacy ORM write",
            priority=Task.Priority.LOW,
        )
        task.refresh_from_db()
        self.assertEqual(task.priority_level_id, levels.get(code="low").pk)
        Task.objects.filter(pk=task.pk).update(priority=Task.Priority.HIGH)
        task.refresh_from_db()
        self.assertEqual(task.priority_level_id, high.pk)
        self.assertEqual(task.priority, Task.Priority.HIGH)

    def test_configuration_page_requires_explicit_workflow_access(self):
        url = reverse("organization-task-configuration", kwargs={"org_id": self.org.pk})
        self.assertRedirects(
            self.client.get(url), f"{reverse('login')}?next={url}",
            fetch_redirect_response=False,
        )
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(url).status_code, 404)
        grant(self.org, self.reader_member, "tasks.view")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(url).status_code, 404)
        grant(self.org, self.reader_member, "tasks.workflow.view")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
