from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.departments.models import Department
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project
from apps.tasks.models import Task
from tests.test_department_memberships import write
from tests.test_projects import grant


class AuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="audit-owner")
        cls.foreign_owner = users.create_superuser(username="audit-foreign-owner")
        cls.viewer = users.create_user(username="audit-viewer")
        cls.scoped = users.create_user(username="audit-scoped")
        cls.ordinary = users.create_user(username="audit-ordinary")
        cls.org = create_organization(user=cls.owner, name="Audit workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign audit workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.foreign_owner_member = cls.foreign.memberships.get(user=cls.foreign_owner)

        def member(user):
            return OrganizationMembership.objects.create(organization=cls.org, user=user)

        cls.viewer_member = member(cls.viewer)
        cls.scoped_member = member(cls.scoped)
        cls.ordinary_member = member(cls.ordinary)
        cls.branch = Department.objects.create(organization=cls.org, name="Audit branch")
        cls.child = Department.objects.create(organization=cls.org, parent=cls.branch, name="Audit child")
        cls.sibling = Department.objects.create(organization=cls.org, name="Audit sibling")
        cls.foreign_department = Department.objects.create(organization=cls.foreign, name="Foreign audit department")
        cls.project = Project.objects.create(
            organization=cls.org, name="Secret audit project", created_by=cls.owner,
            responsible_department=cls.sibling,
        )
        cls.task = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, department=cls.child, name="Visible audit task",
        )
        cls.hidden = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, project=cls.project, department=cls.sibling,
            name="Confidential task for audit search",
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = f"/api/v1/organizations/{self.org.pk}/audit/"

    def list(self, **filters):
        response = self.client.get(self.url, filters)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def task_event(self, task=None, **fields):
        task = task or self.task
        return record(**({
            "organization_id": task.organization_id, "actor_id": self.owner.pk,
            "action": "task.updated", "object_type": "task", "object_id": task.pk,
            "department_id": task.department_id, "project_id": task.project_id,
        } | fields))

    def test_audit_is_atomic_and_idempotent_within_tenant(self):
        operation = uuid4()
        first = self.task_event(idempotency_key="audit:stable", operation_id=operation)
        second = self.task_event(idempotency_key="audit:stable", operation_id=operation)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.operation_id, operation)
        self.assertEqual(AuditEvent.objects.filter(organization=self.org, idempotency_key="audit:stable").count(), 1)

        with self.assertRaisesRegex(RuntimeError, "abort audited operation"):
            with transaction.atomic():
                Task.objects.filter(pk=self.task.pk).update(name="Transient task name")
                saved = self.task_event(idempotency_key="audit:rollback")
                self.assertTrue(AuditEvent.objects.filter(pk=saved.pk).exists())
                raise RuntimeError("abort audited operation")
        self.task.refresh_from_db()
        self.assertEqual(self.task.name, "Visible audit task")
        self.assertFalse(AuditEvent.objects.filter(organization=self.org, idempotency_key="audit:rollback").exists())

    def test_postgresql_prevents_update_delete_and_foreign_scope(self):
        event = self.task_event()
        for mutate in (
            lambda: AuditEvent.objects.filter(pk=event.pk).update(action="task.deleted"),
            lambda: AuditEvent.objects.filter(pk=event.pk).delete(),
            lambda: AuditEvent.objects.create(
                organization=self.org, action="task.updated", object_type="task", object_id=self.task.pk,
                department=self.foreign_department,
            ),
        ):
            with self.subTest(mutation=mutate), self.assertRaises(IntegrityError), transaction.atomic():
                mutate()
        event.refresh_from_db()
        self.assertEqual(event.action, "task.updated")

    def test_api_has_no_write_routes_and_global_superuser_has_no_foreign_access(self):
        event = self.task_event()
        detail_url = f"{self.url}{event.pk}/"
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.client.force_login(self.ordinary)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.owner)
        for method, path in (("post", self.url), ("patch", detail_url), ("delete", detail_url)):
            with self.subTest(method=method):
                self.assertEqual(write(self.client, method, path).status_code, 405)
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).action, "task.updated")

    def test_list_search_filter_counts_and_detail_cannot_probe_hidden_target(self):
        visible = self.task_event()
        hidden = self.task_event(self.hidden)
        grant(self.org, self.viewer_member, "audit.read_all", "audit.read_details", "tasks.view")
        self.client.force_login(self.viewer)
        payload = self.list(object_type="task")
        self.assertEqual([row["id"] for row in payload["items"]], [visible.pk])
        self.assertEqual(payload["total"], 1)
        for query in ({"q": self.hidden.name}, {"project_id": self.project.pk}):
            with self.subTest(query=query):
                result = self.list(**query)
                self.assertEqual(result["items"], [])
                self.assertEqual(result["total"], 0)
        self.assertEqual(self.client.get(f"{self.url}{hidden.pk}/").status_code, 404)
        history = self.client.get(f"{self.url}objects/task/{self.hidden.pk}/")
        self.assertEqual(history.status_code, 404)

    def test_scoped_grant_inherits_descendants_and_loses_access_on_revocation(self):
        visible = self.task_event()
        sibling_task = Task.objects.create(
            organization=self.org, created_by=self.owner_member, department=self.sibling, name="Other branch task",
        )
        sibling = self.task_event(sibling_task)
        assignment = grant(
            self.org, self.scoped_member, "audit.read_scoped", "tasks.view",
            department=self.branch, descendants=True,
        )
        self.client.force_login(self.scoped)
        items = self.list(object_type="task")
        self.assertEqual([row["id"] for row in items["items"]], [visible.pk])
        self.assertEqual(items["total"], 1)
        self.assertEqual(self.client.get(f"{self.url}{sibling.pk}/").status_code, 404)
        detail = self.client.get(f"{self.url}{visible.pk}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        for field in ("changes", "metadata", "operation_id"):
            self.assertIsNone(detail.json()[field])
        assignment.revoked_at = datetime.fromisoformat("2027-11-09T12:00:00+00:00")
        assignment.save(update_fields=["revoked_at"])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(pk=visible.pk).exists())

    def test_filters_and_pagination_use_permitted_records_only(self):
        events = [self.task_event() for _ in range(4)]
        self.client.force_login(self.owner)
        filters = {
            "object_type": "task", "action": "task.updated", "actor_id": self.owner.pk,
            "department_id": self.child.pk, "q": self.task.name, "page_size": 2,
            "date_from": events[0].created_at.date().isoformat(),
            "date_to": events[-1].created_at.date().isoformat(),
        }
        first = self.list(page=1, **filters)
        second = self.list(page=2, **filters)
        self.assertEqual(first["total"], 4)
        self.assertEqual(first["page_size"], 2)
        self.assertEqual(second["page"], 2)
        self.assertEqual(
            [row["id"] for row in first["items"] + second["items"]], [event.pk for event in reversed(events)],
        )
        self.assertEqual(self.list(object_type="task", date_to="2000-01-01")["total"], 0)
        options = self.client.get(f"{self.url}options/")
        self.assertEqual(options.status_code, 200, options.content)
        history = self.client.get(f"{self.url}objects/task/{self.task.pk}/")
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(history.json()["total"], 4)

    def test_scoped_reader_cannot_see_move_that_also_affects_a_sibling_branch(self):
        move = record(
            organization_id=self.org.pk, actor_id=self.owner.pk, action="department.moved",
            object_type="department", object_id=self.child.pk, department_id=self.child.pk,
            changes={"parent_id": {"old": self.sibling.pk, "new": self.branch.pk}},
            metadata={"affected_department_ids": [self.child.pk, self.sibling.pk, self.branch.pk]},
        )
        grant(
            self.org, self.scoped_member, "audit.read_scoped", "audit.read_details",
            department=self.branch, descendants=True,
        )
        self.client.force_login(self.scoped)
        self.assertEqual(self.list(action="department.moved")["total"], 0)
        self.assertEqual(self.client.get(f"{self.url}{move.pk}/").status_code, 404)
        self.client.force_login(self.owner)
        result = self.client.get(f"{self.url}{move.pk}/")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["changes"]["parent_id"], {
            "old": self.sibling.pk, "new": self.branch.pk,
        })

    def test_writer_redacts_unapproved_secrets_but_keeps_safe_change_values(self):
        old = datetime.fromisoformat("2027-11-10T12:00:00+00:00")
        new = datetime.fromisoformat("2027-11-11T13:00:00+00:00")
        event = self.task_event(
            changes={
                "due_at": {"old": old, "new": new},
                "description": {"old": "sensitive original paragraph", "new": "sensitive revised paragraph"},
                "password": {"old": "secret-value-old", "new": "secret-value-new"},
            },
            metadata={"Authorization": "Bearer sensitive-token", "csrf_token": "sensitive-cookie"},
        )
        payload = str((event.changes, event.metadata))
        for secret in (
            "sensitive original paragraph", "sensitive revised paragraph",
            "secret-value-old", "secret-value-new", "sensitive-token", "sensitive-cookie",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, payload)
        due = event.changes["due_at"]
        self.assertEqual(datetime.fromisoformat(due["old"]), old)
        self.assertEqual(datetime.fromisoformat(due["new"]), new)
        self.client.force_login(self.owner)
        detail = self.client.get(f"{self.url}{event.pk}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        self.assertEqual(detail.json()["changes"]["due_at"], due)

    def test_listing_query_count_does_not_grow_per_task(self):
        self.task_event()
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connections["default"]) as initial:
            self.assertEqual(self.list(object_type="task")["total"], 1)
        for index in range(8):
            task = Task.objects.create(
                organization=self.org, created_by=self.owner_member, department=self.child,
                name=f"Audit target {index}",
            )
            self.task_event(task)
        with CaptureQueriesContext(connections["default"]) as expanded:
            self.assertEqual(self.list(object_type="task")["total"], 9)
        self.assertLessEqual(len(expanded), len(initial) + 2)


class ConcurrentAuditTests(TransactionTestCase):
    def test_competing_idempotent_writes_create_one_immutable_event(self):
        owner = get_user_model().objects.create_superuser(username="audit-concurrent-owner")
        organization = create_organization(user=owner, name="Audit concurrency")
        task = Task.objects.create(
            organization=organization, created_by=organization.memberships.get(user=owner), name="Race task",
        )
        with self.assertRaisesRegex(RuntimeError, "business transaction"):
            record(
                organization_id=organization.pk, actor_id=owner.pk, action="task.updated",
                object_type="task", object_id=task.pk,
            )
        start = Barrier(2, timeout=8)
        operation = uuid4()

        def save():
            worker = connections["default"]
            try:
                with transaction.atomic():
                    with worker.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    event = record(
                        organization_id=organization.pk, actor_id=owner.pk, action="task.updated",
                        object_type="task", object_id=task.pk, idempotency_key="audit:race",
                        operation_id=operation,
                    )
                    return event.pk
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save) for _ in range(2)]
            identifiers = [future.result(timeout=16) for future in futures]
        self.assertEqual(identifiers[0], identifiers[1])
        event = AuditEvent.objects.get(organization=organization, idempotency_key="audit:race")
        self.assertEqual(event.operation_id, operation)
