from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.access.models import RolePermission
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from tests.test_collection_pages import CollectionMarkup
from tests.test_projects import grant


class DepartmentStructureUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="structure-ui-owner")
        cls.foreign_owner = users.create_superuser(username="structure-ui-foreign-owner")
        cls.reader = users.create_user(username="structure-ui-reader")
        cls.creator = users.create_user(username="structure-ui-creator")
        cls.organization = create_organization(user=cls.owner, name="Structure UI workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign structure workspace")
        cls.reader_member = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.reader,
        )
        cls.creator_member = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.creator,
        )
        cls.root = Department.objects.create(organization=cls.organization, name="Main branch")
        cls.child = Department.objects.create(
            organization=cls.organization, name="Nested branch", parent=cls.root,
        )
        cls.leaf = Department.objects.create(
            organization=cls.organization, name="Leaf branch", parent=cls.child,
        )
        cls.sibling = Department.objects.create(
            organization=cls.organization, name="Unrelated branch",
        )
        cls.archived = Department.objects.create(
            organization=cls.organization, name="Archived branch", is_archived=True,
        )
        cls.foreign_department = Department.objects.create(
            organization=cls.foreign, name="Foreign department",
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.page_url = reverse("organization-departments", kwargs={"org_id": self.organization.pk})
        self.api_url = reverse("api-1.0.0:department_list", kwargs={"org_id": self.organization.pk})
        self.actions_url = reverse("api-1.0.0:department_actions", kwargs={"org_id": self.organization.pk})

    def markup(self):
        response = self.client.get(self.page_url)
        self.assertEqual(response.status_code, 200, response.content)
        return CollectionMarkup(response)

    def actions(self):
        response = self.client.get(self.actions_url)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def member_page(self, department):
        return reverse(
            "department-members",
            kwargs={"org_id": self.organization.pk, "department_id": department.pk},
        )

    def create(self, parent=None, *, csrf=True):
        headers = {}
        if csrf:
            headers["X-CSRFToken"] = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        return self.client.post(
            self.api_url, {"name": "Created from structure", "parent_id": parent.pk if parent else None},
            content_type="application/json", headers=headers,
        )

    def test_owner_structure_uses_closed_labelled_dialog_and_compact_controls(self):
        self.client.force_login(self.owner)
        markup = self.markup()
        self.assertEqual(markup.ids["department-app"][1]["data-api-url"], self.api_url)
        self.assertEqual(markup.ids["department-app"][1]["data-actions-url"], self.actions_url)
        self.assertIn("department-tree", markup.ids)
        self.assertIn("department-tree-heading", markup.ids)

        trigger, attrs = markup.ids["department-create-open"]
        self.assertEqual(trigger, "button")
        self.assertEqual(attrs["type"], "button")
        self.assertIn("hidden", attrs)
        self.assertEqual(attrs["aria-haspopup"], "dialog")
        self.assertEqual(attrs["aria-controls"], "department-editor")
        dialog, attrs = markup.ids["department-editor"]
        self.assertEqual(dialog, "dialog")
        self.assertNotIn("open", attrs)
        self.assertIn(attrs["aria-labelledby"], markup.ids)
        container, fields = markup.forms["department-form"]
        self.assertEqual(container, "department-editor")
        self.assertTrue({"name", "parent_id", "description"} <= fields)
        for field in ("department-name", "department-parent", "department-description"):
            self.assertIn(field, markup.labels)

        search = "department-search"
        self.assertTrue(search in markup.labels or markup.ids[search][1].get("aria-label"))
        filter_tag, attrs = markup.ids["department-filters"]
        self.assertEqual(filter_tag, "details")
        self.assertNotIn("open", attrs)
        self.assertIn("department-show-archived", markup.ids)
        self.assertEqual(markup.ids["department-status"][1]["role"], "status")
        self.assertEqual(markup.ids["department-load-error"][1]["role"], "alert")
        self.assertEqual(markup.ids["department-form-error"][1]["role"], "alert")

    def test_structure_keeps_login_tenant_and_read_only_mutation_boundaries(self):
        self.assertRedirects(
            self.client.get(self.page_url), f"{reverse('login')}?next={self.page_url}",
            fetch_redirect_response=False,
        )
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.page_url).status_code, 404)
        self.assertEqual(self.client.get(self.actions_url).status_code, 404)

        self.client.force_login(self.reader)
        markup = self.markup()
        self.assertNotIn("department-editor", markup.ids)
        self.assertNotIn("department-create-open", markup.ids)
        data = self.actions()
        self.assertFalse(data["root"]["create"])
        node = data["nodes"][str(self.root.pk)]
        for action in ("create", "update", "move", "archive"):
            self.assertFalse(node[action])
        self.assertEqual(self.create().status_code, 403)
        self.assertFalse(self.organization.departments.filter(name="Created from structure").exists())

    def test_child_only_create_keeps_root_action_disabled_and_csrf_required(self):
        grant(self.organization, self.creator_member, "departments.create", department=self.root)
        self.client.force_login(self.creator)
        markup = self.markup()
        self.assertIn("department-editor", markup.ids)
        self.assertIn("hidden", markup.ids["department-create-open"][1])
        data = self.actions()
        self.assertFalse(data["root"]["create"])
        self.assertTrue(data["nodes"][str(self.root.pk)]["create"])
        self.assertFalse(data["nodes"][str(self.child.pk)]["create"])

        self.assertEqual(self.create(csrf=False, parent=self.root).status_code, 403)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.create(parent=self.child).status_code, 403)
        self.assertEqual(self.create(parent=self.foreign_department).status_code, 404)
        response = self.create(parent=self.root)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["parent_id"], self.root.pk)

    def test_member_links_follow_branch_membership_and_exclude_archived_or_foreign(self):
        self.client.force_login(self.reader)
        data = self.actions()
        self.assertFalse(data["nodes"][str(self.root.pk)]["open_members"])
        self.assertEqual(self.client.get(self.member_page(self.root)).status_code, 404)

        DepartmentMembership.objects.create(
            organization=self.organization, department=self.leaf,
            organization_membership=self.reader_member,
        )
        data = self.actions()
        for department in (self.root, self.child, self.leaf):
            with self.subTest(department=department.pk):
                self.assertTrue(data["nodes"][str(department.pk)]["open_members"])
                self.assertEqual(self.client.get(self.member_page(department)).status_code, 200)
        self.assertFalse(data["nodes"][str(self.sibling.pk)]["open_members"])
        for department in (self.archived, self.foreign_department):
            with self.subTest(department=department.pk):
                self.assertNotIn(str(department.pk), data["nodes"])
                self.assertEqual(self.client.get(self.member_page(department)).status_code, 404)

    def test_member_links_follow_scoped_grants_and_full_directory_policy(self):
        role = grant(
            self.organization, self.reader_member, "departments.directory.read", department=self.root,
        ).role
        self.client.force_login(self.reader)
        data = self.actions()
        self.assertTrue(data["nodes"][str(self.root.pk)]["open_members"])
        self.assertFalse(data["nodes"][str(self.child.pk)]["open_members"])
        self.assertEqual(self.client.get(self.member_page(self.child)).status_code, 404)

        RolePermission.objects.filter(role=role).update(include_descendants=True)
        data = self.actions()
        self.assertTrue(data["nodes"][str(self.child.pk)]["open_members"])
        self.assertEqual(self.client.get(self.member_page(self.child)).status_code, 200)
        self.assertFalse(data["nodes"][str(self.sibling.pk)]["open_members"])

        Organization.objects.filter(pk=self.organization.pk).update(
            department_directory_visibility=Organization.DepartmentDirectoryVisibility.ORGANIZATION,
        )
        data = self.actions()
        self.assertTrue(data["nodes"][str(self.sibling.pk)]["open_members"])
        self.assertEqual(self.client.get(self.member_page(self.sibling)).status_code, 200)
