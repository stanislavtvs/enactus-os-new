from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.departments.models import Department
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization


class DepartmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="departments-owner")
        cls.other_owner = users.create_superuser(username="departments-foreign-owner")
        cls.member = users.create_user(username="departments-member")
        cls.manager = users.create_user(username="departments-manager")
        cls.inactive = users.create_user(username="departments-inactive")
        cls.organization = create_organization(user=cls.owner, name="Department workspace")
        cls.other_organization = create_organization(
            user=cls.other_owner, name="Foreign department workspace"
        )
        for user, active, can_manage in (
            (cls.member, True, False),
            (cls.manager, True, True),
            (cls.inactive, False, False),
        ):
            OrganizationMembership.objects.create(
                organization=cls.organization,
                user=user,
                is_active=active,
                can_manage_members=can_manage,
            )
        cls.root = Department.objects.create(organization=cls.organization, name="Root department")
        cls.other_root = Department.objects.create(
            organization=cls.other_organization, name="Foreign department"
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.api_url = f"/api/v1/organizations/{self.organization.pk}/departments/"

    def detail_url(self, department):
        return f"{self.api_url}{department.pk}/"

    def write(self, method, url, data=None, *, csrf=True):
        headers = {}
        if csrf:
            self.client.get(reverse("login"))
            headers["X-CSRFToken"] = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        if data is None:
            return getattr(self.client, method)(url, headers=headers)
        return getattr(self.client, method)(
            url, data, content_type="application/json", headers=headers
        )

    def create_department(self, name, parent_id):
        response = self.write(
            "post",
            self.api_url,
            {"name": name, "description": f"{name} description", "parent_id": parent_id},
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(
            set(data),
            {"id", "parent_id", "name", "description", "is_archived", "created_at", "updated_at"},
        )
        self.assertEqual(data["parent_id"], parent_id)
        self.assertEqual(data["name"], name)
        self.assertFalse(data["is_archived"])
        return Department.objects.get(pk=data["id"])

    def test_owner_creates_nested_departments_and_moves_node_to_root(self):
        self.client.force_login(self.owner)
        child = self.create_department("Child", self.root.pk)
        grandchild = self.create_department("Grandchild", child.pk)
        leaf = self.create_department("Leaf", grandchild.pk)

        response = self.write(
            "patch", self.detail_url(child), {"parent_id": None, "name": "Moved"}
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["parent_id"])
        self.assertEqual(response.json()["name"], "Moved")
        self.assertEqual(response.json()["description"], "Child description")
        leaf.refresh_from_db()
        self.assertEqual(leaf.parent_id, grandchild.pk)
        rows = {row["id"]: row for row in self.client.get(self.api_url).json()}
        self.assertEqual(set(rows), {self.root.pk, child.pk, grandchild.pk, leaf.pk})
        self.assertEqual(rows[grandchild.pk]["parent_id"], child.pk)

    def test_member_read_allowed_but_all_writes_forbidden(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.api_url).status_code, 200)
        self.assertEqual(self.client.get(self.detail_url(self.root)).status_code, 200)
        for method, url, data in (
            ("post", self.api_url, {"name": "Unauthorized"}),
            ("patch", self.detail_url(self.root), {"name": "Unauthorized"}),
            ("delete", self.detail_url(self.root), None),
        ):
            with self.subTest(method=method):
                self.assertEqual(self.write(method, url, data).status_code, 403)
        self.root.refresh_from_db()
        self.assertEqual(self.root.name, "Root department")
        self.assertFalse(self.root.is_archived)
        self.assertFalse(Department.objects.filter(name="Unauthorized").exists())

    def test_delegated_active_member_can_create_rename_and_archive(self):
        self.client.force_login(self.manager)
        child = self.create_department("Managed", self.root.pk)
        response = self.write(
            "patch", self.detail_url(child), {"name": "Renamed", "description": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Renamed")
        self.assertEqual(response.json()["description"], "")
        self.assertEqual(self.write("delete", self.detail_url(child)).status_code, 204)
        child.refresh_from_db()
        self.assertTrue(child.is_archived)

    def test_foreign_and_inactive_membership_cannot_access_organization_routes(self):
        for user in (self.other_owner, self.inactive):
            self.client.force_login(user)
            for method, url, data in (
                ("get", self.api_url, None),
                ("get", self.detail_url(self.root), None),
                ("post", self.api_url, {"name": "Unauthorized"}),
                ("patch", self.detail_url(self.root), {"name": "Unauthorized"}),
                ("delete", self.detail_url(self.root), None),
            ):
                with self.subTest(user=user.username, method=method, url=url):
                    self.assertEqual(self.write(method, url, data).status_code, 404)

    def test_foreign_department_and_parent_ids_are_hidden(self):
        self.client.force_login(self.owner)
        foreign_url = self.detail_url(self.other_root)
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
        self.assertEqual(self.write("patch", foreign_url, {"name": "Foreign write"}).status_code, 404)
        self.assertEqual(self.write("delete", foreign_url).status_code, 404)
        self.assertEqual(
            self.write("post", self.api_url, {"name": "Invalid", "parent_id": self.other_root.pk})
            .status_code,
            404,
        )
        self.assertEqual(
            self.write("patch", self.detail_url(self.root), {"parent_id": self.other_root.pk})
            .status_code,
            404,
        )
        self.root.refresh_from_db()
        self.other_root.refresh_from_db()
        self.assertIsNone(self.root.parent_id)
        self.assertFalse(self.other_root.is_archived)
        self.assertEqual(self.other_root.name, "Foreign department")

    def test_self_parent_and_ancestor_cycle_are_rejected(self):
        child = Department.objects.create(
            organization=self.organization, parent=self.root, name="Child"
        )
        leaf = Department.objects.create(
            organization=self.organization, parent=child, name="Leaf"
        )
        self.client.force_login(self.owner)
        for candidate in (self.root, child, leaf):
            with self.subTest(candidate=candidate.pk):
                response = self.write(
                    "patch", self.detail_url(self.root), {"parent_id": candidate.pk}
                )
                self.assertEqual(response.status_code, 409)
        self.root.refresh_from_db()
        self.assertIsNone(self.root.parent_id)

    def test_patch_only_accepts_editable_fields_and_rejects_null_text(self):
        self.client.force_login(self.owner)
        for payload in (
            {"organization_id": self.other_organization.pk},
            {"is_archived": True},
            {"name": None},
            {"description": None},
        ):
            with self.subTest(payload=payload):
                response = self.write("patch", self.detail_url(self.root), payload)
                self.assertEqual(response.status_code, 422)
        self.root.refresh_from_db()
        self.assertEqual(self.root.organization_id, self.organization.pk)
        self.assertEqual(self.root.name, "Root department")
        self.assertEqual(self.root.description, "")
        self.assertFalse(self.root.is_archived)

    def test_archive_preserves_subtree_and_blocks_edits_or_new_children(self):
        child = Department.objects.create(
            organization=self.organization, parent=self.root, name="Child"
        )
        grandchild = Department.objects.create(
            organization=self.organization, parent=child, name="Grandchild"
        )
        sibling = Department.objects.create(
            organization=self.organization, parent=self.root, name="Sibling"
        )
        self.client.force_login(self.owner)

        response = self.write("delete", self.detail_url(child))

        self.assertEqual(response.status_code, 204)
        rows = {row["id"]: row for row in self.client.get(self.api_url).json()}
        self.assertEqual(set(rows), {self.root.pk, child.pk, grandchild.pk, sibling.pk})
        self.assertEqual(
            {row["id"] for row in rows.values() if row["is_archived"]},
            {child.pk, grandchild.pk},
        )
        self.assertEqual(rows[grandchild.pk]["parent_id"], child.pk)
        self.assertTrue(self.client.get(self.detail_url(grandchild)).json()["is_archived"])
        self.assertEqual(
            self.write("patch", self.detail_url(grandchild), {"name": "Denied"}).status_code,
            409,
        )
        self.assertEqual(
            self.write("patch", self.detail_url(sibling), {"parent_id": child.pk}).status_code,
            409,
        )
        self.assertEqual(
            self.write("post", self.api_url, {"name": "Denied", "parent_id": child.pk}).status_code,
            409,
        )

    def test_api_requires_authentication_and_csrf(self):
        self.assertEqual(self.client.get(self.api_url).status_code, 401)
        self.assertEqual(self.client.get(self.detail_url(self.root)).status_code, 401)
        self.assertEqual(
            self.write("post", self.api_url, {"name": "Anonymous"}).status_code, 401
        )
        self.client.force_login(self.owner)
        for method, url, data in (
            ("post", self.api_url, {"name": "No CSRF"}),
            ("patch", self.detail_url(self.root), {"name": "No CSRF"}),
            ("delete", self.detail_url(self.root), None),
        ):
            with self.subTest(method=method):
                self.assertEqual(self.write(method, url, data, csrf=False).status_code, 403)
        self.root.refresh_from_db()
        self.assertFalse(self.root.is_archived)
        self.assertFalse(Department.objects.filter(name="No CSRF").exists())

    def test_ui_requires_active_membership_and_exposes_scoped_permissions(self):
        url = reverse("organization-departments", kwargs={"org_id": self.organization.pk})
        self.assertRedirects(
            self.client.get(url), f"{reverse('login')}?next={url}", fetch_redirect_response=False
        )
        for user, status, can_manage in (
            (self.owner, 200, True),
            (self.manager, 200, True),
            (self.member, 200, False),
            (self.other_owner, 404, False),
            (self.inactive, 404, False),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, status)
                if status == 200:
                    self.assertEqual(response.context["can_manage_departments"], can_manage)
                    self.assertNotContains(response, self.other_root.name)

    def check_immediate_constraints(self):
        # TestCase keeps an outer transaction open, so flush deferred checks explicitly.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_database_rejects_cross_organization_parent_even_without_service(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Department.objects.filter(pk=self.root.pk).update(parent=self.other_root)
            self.check_immediate_constraints()
        self.root.refresh_from_db()
        self.assertIsNone(self.root.parent_id)

    def test_database_rejects_multirow_bulk_update_cycle(self):
        sibling = Department.objects.create(organization=self.organization, name="Sibling")
        self.root.parent_id = sibling.pk
        sibling.parent_id = self.root.pk
        with self.assertRaises(IntegrityError), transaction.atomic():
            Department.objects.bulk_update([self.root, sibling], ["parent"])
            self.check_immediate_constraints()
        self.root.refresh_from_db()
        sibling.refresh_from_db()
        self.assertIsNone(self.root.parent_id)
        self.assertIsNone(sibling.parent_id)

    def test_database_rejects_multirow_bulk_insert_cycle(self):
        first_id = max(self.root.pk, self.other_root.pk) + 1000
        second_id = first_id + 1
        with self.assertRaises(IntegrityError), transaction.atomic():
            Department.objects.bulk_create(
                [
                    Department(
                        id=first_id, organization=self.organization, parent_id=second_id, name="A"
                    ),
                    Department(
                        id=second_id, organization=self.organization, parent_id=first_id, name="B"
                    ),
                ]
            )
            self.check_immediate_constraints()
        self.assertFalse(Department.objects.filter(pk__in=[first_id, second_id]).exists())


class DepartmentConcurrencyTests(TransactionTestCase):
    def test_concurrent_moves_cannot_commit_a_cycle(self):
        owner = get_user_model().objects.create_superuser(username="department-race-owner")
        organization = create_organization(user=owner, name="Department race workspace")
        first = Department.objects.create(organization=organization, name="First")
        second = Department.objects.create(organization=organization, name="Second")
        start = Barrier(2, timeout=5)

        def move(department_id, parent_id):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    Department.objects.filter(pk=department_id).update(parent_id=parent_id)
            except IntegrityError as exc:
                return exc.__cause__.diag.constraint_name
            finally:
                worker_connection.close()
            return "committed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_move = pool.submit(move, first.pk, second.pk)
            second_move = pool.submit(move, second.pk, first.pk)
            self.assertCountEqual(
                [first_move.result(timeout=12), second_move.result(timeout=12)],
                ["committed", "department_tree_no_cycle"],
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIn((first.parent_id, second.parent_id), {(second.pk, None), (None, first.pk)})
