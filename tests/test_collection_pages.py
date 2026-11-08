from html.parser import HTMLParser

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project
from apps.tasks.models import Task
from tests.test_projects import grant


class CollectionMarkup(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.ids = {}
        self.modes = {}
        self.labels = set()
        self.forms = {}
        self._dialog = None
        self._form = None
        self.feed(response.content.decode(response.charset))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        node_id = attrs.get("id")
        if node_id:
            self.ids[node_id] = (tag, attrs)
        mode = attrs.get("data-collection-mode")
        if mode:
            self.modes[mode] = (tag, attrs)
        if tag == "label" and attrs.get("for"):
            self.labels.add(attrs["for"])
        if tag == "dialog":
            self._dialog = node_id
        if tag == "form" and node_id:
            self._form = node_id
            self.forms[node_id] = (self._dialog, set())
        if self._form and tag in ("input", "select", "textarea") and attrs.get("name"):
            self.forms[self._form][1].add(attrs["name"])

    def handle_endtag(self, tag):
        if tag == "dialog":
            self._dialog = None
        if tag == "form":
            self._form = None


class CollectionPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="collection-owner")
        cls.reader = users.create_user(username="collection-reader")
        cls.foreign_owner = users.create_superuser(username="collection-foreign-owner")
        cls.organization = create_organization(user=cls.owner, name="Collection workspace")
        cls.foreign = create_organization(user=cls.foreign_owner, name="Foreign collection workspace")
        cls.reader_member = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.reader,
        )
        grant(cls.organization, cls.reader_member, "projects.view", "tasks.view")
        cls.project = Project.objects.create(
            organization=cls.organization, name="Existing collection project", created_by=cls.owner,
        )
        cls.task = Task.objects.create(
            organization=cls.organization, name="Existing collection task",
            created_by=cls.organization.memberships.get(user=cls.owner),
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.api = f"/api/v1/organizations/{self.organization.pk}/"

    def page_url(self, plural, organization=None):
        return reverse(
            f"organization-{plural}", kwargs={"org_id": (organization or self.organization).pk}
        )

    def page_markup(self, plural):
        response = self.client.get(self.page_url(plural))
        self.assertEqual(response.status_code, 200, response.content)
        return CollectionMarkup(response)

    def api_write(self, method, url, payload=None, *, csrf=True):
        headers = {}
        if csrf:
            headers["X-CSRFToken"] = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        if payload is None:
            return getattr(self.client, method)(url, headers=headers)
        return getattr(self.client, method)(
            url, payload, content_type="application/json", headers=headers,
        )

    def test_collection_forms_are_closed_dialogs_with_existing_input_fields(self):
        self.client.force_login(self.owner)
        forms = {
            "project": {
                "name", "description", "status", "start_date", "planned_end_date",
                "responsible_department_id", "lead_membership_id",
            },
            "task": {
                "name", "description", "priority_id", "planned_start_at", "due_at",
                "project_id", "department_id",
            },
        }
        for singular, fields in forms.items():
            with self.subTest(singular=singular):
                markup = self.page_markup(f"{singular}s")
                trigger, trigger_attrs = markup.ids[f"{singular}-create-open"]
                self.assertEqual(trigger, "button")
                self.assertIn("hidden", trigger_attrs)
                self.assertEqual(trigger_attrs["type"], "button")

                panel_id = f"{singular}-create-panel"
                self.assertEqual(trigger_attrs["aria-haspopup"], "dialog")
                self.assertEqual(trigger_attrs["aria-controls"], panel_id)
                dialog, attrs = markup.ids[panel_id]
                self.assertEqual(dialog, "dialog")
                self.assertNotIn("open", attrs)
                self.assertIn(attrs["aria-labelledby"], markup.ids)
                form_id = f"{singular}-create-form"
                self.assertEqual(markup.ids[form_id][1]["method"].lower(), "post")
                containing_dialog, names = markup.forms[form_id]
                self.assertEqual(containing_dialog, panel_id)
                self.assertTrue(fields <= names, fields - names)

    def test_modes_filters_and_announcements_have_accessible_markup(self):
        self.client.force_login(self.owner)
        for plural in ("projects", "tasks"):
            with self.subTest(plural=plural):
                markup = self.page_markup(plural)
                self.assertEqual(set(markup.modes), {"list", "grid"})
                self.assertCountEqual(
                    [attrs["aria-pressed"] for _, attrs in markup.modes.values()], ["true", "false"],
                )
                for tag, attrs in markup.modes.values():
                    self.assertEqual(tag, "button")
                    self.assertEqual(attrs["type"], "button")
                    self.assertTrue(attrs["aria-label"].strip())
                    self.assertTrue(attrs["title"].strip())
                filter_tag, filter_attrs = markup.ids[f"{plural}-filters"]
                self.assertEqual(filter_tag, "details")
                self.assertNotIn("open", filter_attrs)
                search = f"{plural}-search"
                self.assertTrue(search in markup.labels or markup.ids[search][1].get("aria-label"))
                self.assertEqual(markup.ids[f"{plural}-status"][1]["role"], "status")
                self.assertEqual(markup.ids[f"{plural}-error"][1]["role"], "alert")
                notice = "projects-feedback" if plural == "projects" else "tasks-notice"
                self.assertEqual(markup.ids[notice][1]["role"], "status")

    def test_collection_pages_keep_login_and_tenant_boundary(self):
        for plural in ("projects", "tasks"):
            with self.subTest(plural=plural):
                self.client.logout()
                url = self.page_url(plural)
                self.assertRedirects(
                    self.client.get(url), f"{reverse('login')}?next={url}",
                    fetch_redirect_response=False,
                )
                self.client.force_login(self.reader)
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(
                    self.client.get(self.page_url(plural, self.foreign)).status_code, 404,
                )

    def test_read_only_collection_controls_do_not_bypass_api_rbac(self):
        self.client.force_login(self.reader)
        for singular, item in (("project", self.project), ("task", self.task)):
            with self.subTest(singular=singular):
                plural = f"{singular}s"
                markup = self.page_markup(plural)
                self.assertIn("hidden", markup.ids[f"{singular}-create-open"][1])
                options = self.client.get(f"{self.api}{singular}-options/").json()
                create_key = "can_create_independent" if singular == "project" else "can_create_standalone"
                self.assertFalse(options[create_key])
                response = self.api_write("post", f"{self.api}{plural}/", {"name": "No bypass"})
                self.assertEqual(response.status_code, 403, response.content)
                detail = f"{self.api}{plural}/{item.pk}/"
                self.assertEqual(self.api_write("patch", detail, {"name": "No bypass"}).status_code, 403)
                self.assertEqual(self.api_write("delete", detail).status_code, 403)
                item.refresh_from_db()
                self.assertFalse(item.is_archived)

    def test_collection_page_supplies_csrf_for_create_and_archive(self):
        self.client.force_login(self.owner)
        for plural in ("projects", "tasks"):
            with self.subTest(plural=plural):
                self.page_markup(plural)
                self.assertIn(settings.CSRF_COOKIE_NAME, self.client.cookies)
                url = f"{self.api}{plural}/"
                self.assertEqual(
                    self.api_write("post", url, {"name": "Missing token"}, csrf=False).status_code,
                    403,
                )
                created = self.api_write("post", url, {"name": f"Created from {plural}"})
                self.assertEqual(created.status_code, 201, created.content)
                detail = f"{url}{created.json()['id']}/"
                self.assertEqual(self.api_write("delete", detail, csrf=False).status_code, 403)
                self.assertEqual(self.api_write("delete", detail).status_code, 204)
                self.assertTrue(self.client.get(detail).json()["is_archived"])
