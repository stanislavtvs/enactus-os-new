from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, connections, transaction
from django.http import Http404
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.access.models import Role, RoleAssignment, RolePermission
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectDepartmentLink
from apps.projects.services import set_responsible_department
from tests.test_department_memberships import write


API = "/api/v1/organizations/"


def grant(organization, membership, *codes, department=None, descendants=False):
    role = Role.objects.create(organization=organization, name="Project permission set")
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, code=code, include_descendants=descendants) for code in codes]
    )
    scope = None
    if department is not None:
        scope, _ = DepartmentMembership.objects.get_or_create(
            organization=organization, organization_membership=membership, department=department
        )
    return RoleAssignment.objects.create(
        organization=organization, role=role, organization_membership=membership,
        department_membership=scope,
    )


class ProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="projects-owner")
        cls.foreign_owner = users.create_superuser(username="projects-foreign-owner")
        cls.operator = users.create_user(username="projects-operator")
        cls.lead = users.create_user(username="projects-lead")
        cls.disabled = users.create_user(username="projects-disabled")
        cls.org = create_organization(user=cls.owner, name="Project workspace")
        cls.other_org = create_organization(user=cls.foreign_owner, name="Foreign workspace")
        cls.operator_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.operator
        )
        cls.lead_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.lead
        )
        cls.inactive_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.disabled, is_active=False
        )
        cls.foreign_membership = cls.other_org.memberships.get(user=cls.foreign_owner)
        cls.root = Department.objects.create(organization=cls.org, name="Root")
        cls.child = Department.objects.create(organization=cls.org, name="Child", parent=cls.root)
        cls.sibling = Department.objects.create(organization=cls.org, name="Sibling")
        cls.foreign_department = Department.objects.create(organization=cls.other_org, name="Foreign")
        cls.archived_department = Department.objects.create(
            organization=cls.org, name="Archived department", is_archived=True
        )
        cls.local = Project.objects.create(
            organization=cls.org, name="Local project", created_by=cls.owner,
            responsible_department=cls.child, status=Project.Status.ACTIVE,
        )
        cls.outside = Project.objects.create(
            organization=cls.org, name="Outside project", created_by=cls.owner,
            responsible_department=cls.sibling, lead_membership=cls.lead_membership,
        )
        cls.independent = Project.objects.create(
            organization=cls.org, name="Independent project", created_by=cls.owner
        )
        cls.foreign_project = Project.objects.create(
            organization=cls.other_org, name="Foreign project", created_by=cls.foreign_owner,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.list_url = f"{self.org_url}projects/"

    def project_url(self, project, suffix=""):
        return f"{self.list_url}{project.pk}/{suffix}"

    def test_create_independent_project_and_validate_dates_and_status(self):
        self.client.force_login(self.owner)
        response = write(
            self.client, "post", self.list_url,
            {
                "name": "  New independent project  ",
                "description": "Description",
                "start_date": "2026-11-10",
                "planned_end_date": "2026-11-11",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["name"], "New independent project")
        self.assertEqual(data["status"], "draft")
        self.assertIsNone(data["lead"])
        self.assertIsNone(data["responsible_department"])
        project = Project.objects.get(pk=data["id"])
        self.assertEqual(project.created_by_id, self.owner.pk)
        self.assertEqual(project.organization_id, self.org.pk)

        url = self.project_url(project)
        response = write(self.client, "patch", url, {"status": "paused", "description": ""})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "paused")
        self.assertEqual(response.json()["description"], "")
        for payload in (
            {"status": "unknown"},
            {"planned_end_date": "2026-11-09"},
            {"start_date": "2026-11-12"},
            {"name": "\u0000"},
            {"organization_id": self.other_org.pk},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(write(self.client, "patch", url, payload).status_code, 422)
        project.refresh_from_db()
        self.assertEqual(project.planned_end_date.isoformat(), "2026-11-11")
        self.assertEqual(project.start_date.isoformat(), "2026-11-10")

    def test_create_project_with_responsible_department_and_lead(self):
        self.client.force_login(self.owner)
        response = write(
            self.client, "post", self.list_url,
            {
                "name": "With lead and department",
                "responsible_department_id": self.child.pk,
                "lead_membership_id": self.lead_membership.pk,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        project = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(project.responsible_department_id, self.child.pk)
        self.assertEqual(project.lead_membership_id, self.lead_membership.pk)
        self.assertFalse(project.department_links.exists())
        self.assertEqual(response.json()["lead"]["id"], self.lead_membership.pk)
        self.assertEqual(response.json()["responsible_department"]["id"], self.child.pk)

    def test_scoped_create_cannot_attach_unmanaged_department_or_lead(self):
        self.client.force_login(self.operator)
        grant(
            self.org, self.operator_membership, "projects.view", "projects.create",
            department=self.child,
        )
        payload = {"name": "Scoped project", "responsible_department_id": self.child.pk}
        self.assertEqual(write(self.client, "post", self.list_url, payload).status_code, 403)

        grant(
            self.org, self.operator_membership, "projects.manage_departments", department=self.child
        )
        for changes in (
            {"responsible_department_id": None},
            {"responsible_department_id": self.sibling.pk},
            {"lead_membership_id": self.lead_membership.pk},
        ):
            with self.subTest(changes=changes):
                response = write(self.client, "post", self.list_url, {**payload, **changes})
                self.assertEqual(response.status_code, 403)

        response = write(self.client, "post", self.list_url, payload)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Project.objects.filter(name="Scoped project").count(), 1)

    def test_unauthenticated_foreign_and_inactive_access_and_csrf(self):
        local_url = self.project_url(self.local)
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.assertEqual(self.client.get(local_url).status_code, 401)
        self.assertEqual(
            write(self.client, "post", self.list_url, {"name": "No session"}).status_code, 401
        )
        for user in (self.foreign_owner, self.disabled):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.list_url).status_code, 404)
            self.assertEqual(self.client.get(local_url).status_code, 404)
            self.assertEqual(write(self.client, "delete", local_url).status_code, 404)

        self.client.force_login(self.owner)
        for method, url, data in (
            ("post", self.list_url, {"name": "No CSRF"}),
            ("patch", local_url, {"name": "No CSRF"}),
            ("delete", local_url, None),
            ("put", f"{local_url}lead/", {"lead_membership_id": self.lead_membership.pk}),
            ("put", f"{local_url}departments/{self.sibling.pk}/", None),
        ):
            with self.subTest(method=method, url=url):
                self.assertEqual(write(self.client, method, url, data, csrf=False).status_code, 403)

        self.assertEqual(self.client.get(self.project_url(self.foreign_project)).status_code, 404)

    def test_lead_and_participating_department_do_not_grant_project_visibility(self):
        ProjectDepartmentLink.objects.create(
            organization=self.org, project=self.outside, department=self.root
        )
        grant(self.org, self.operator_membership, "projects.view", department=self.root, descendants=True)

        self.client.force_login(self.operator)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({row["id"] for row in response.json()}, {self.local.pk})
        self.assertEqual(self.client.get(self.project_url(self.local)).status_code, 200)
        self.assertEqual(self.client.get(self.project_url(self.outside)).status_code, 404)
        self.assertEqual(self.client.get(self.project_url(self.independent)).status_code, 404)

        self.client.force_login(self.lead)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(self.client.get(self.project_url(self.outside)).status_code, 404)

    def test_project_pages_match_the_api_visibility_boundary(self):
        list_url = reverse("organization-projects", kwargs={"org_id": self.org.pk})
        detail_url = reverse(
            "organization-project", kwargs={"org_id": self.org.pk, "project_id": self.local.pk}
        )
        self.assertRedirects(
            self.client.get(list_url), f"{reverse('login')}?next={list_url}",
            fetch_redirect_response=False,
        )
        self.client.force_login(self.lead)
        self.assertEqual(self.client.get(list_url).status_code, 404)
        self.assertEqual(self.client.get(detail_url).status_code, 404)

        grant(self.org, self.operator_membership, "projects.view", department=self.child)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(list_url).status_code, 200)
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        foreign_url = reverse(
            "organization-project",
            kwargs={"org_id": self.org.pk, "project_id": self.foreign_project.pk},
        )
        self.assertEqual(self.client.get(foreign_url).status_code, 404)

    def test_mutation_needs_view_and_the_specific_action(self):
        grant(self.org, self.operator_membership, "projects.update", department=self.child)
        self.client.force_login(self.operator)
        url = self.project_url(self.local)
        self.assertEqual(write(self.client, "patch", url, {"name": "Hidden"}).status_code, 404)

        grant(self.org, self.operator_membership, "projects.view", department=self.child)
        self.assertEqual(write(self.client, "patch", url, {"name": "Renamed"}).status_code, 200)
        self.assertEqual(
            write(self.client, "put", f"{url}lead/", {"lead_membership_id": self.lead_membership.pk})
            .status_code,
            403,
        )
        self.assertEqual(write(self.client, "delete", url).status_code, 403)

    def test_responsibility_change_needs_source_and_destination_scope_and_keeps_links(self):
        ProjectDepartmentLink.objects.create(
            organization=self.org, project=self.local, department=self.sibling
        )
        grant(
            self.org, self.operator_membership, "projects.view", "projects.manage_departments",
            department=self.root, descendants=True,
        )
        self.client.force_login(self.operator)
        url = self.project_url(self.local, "responsible-department/")
        for destination in (self.sibling.pk, None):
            with self.subTest(destination=destination):
                self.assertEqual(
                    write(self.client, "put", url, {"responsible_department_id": destination})
                    .status_code,
                    403,
                )
        self.local.refresh_from_db()
        self.assertEqual(self.local.responsible_department_id, self.child.pk)

        response = write(
            self.client, "put", url, {"responsible_department_id": self.root.pk}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["responsible_department"]["id"], self.root.pk)
        self.assertEqual(list(self.local.department_links.values_list("department_id", flat=True)),
                         [self.sibling.pk])

    def test_participating_department_link_is_idempotent_and_separate_from_responsible(self):
        self.client.force_login(self.owner)
        url = self.project_url(self.local, f"departments/{self.sibling.pk}/")
        for _ in range(2):
            self.assertEqual(write(self.client, "put", url).status_code, 204)
        self.assertEqual(self.local.department_links.count(), 1)
        self.local.refresh_from_db()
        self.assertEqual(self.local.responsible_department_id, self.child.pk)
        self.assertEqual(write(self.client, "delete", url).status_code, 204)
        self.assertFalse(self.local.department_links.exists())

    def test_project_options_expose_only_authorized_active_lead_candidates(self):
        url = f"{self.org_url}project-options/"
        query = {"project_id": self.local.pk}
        grant(self.org, self.operator_membership, "projects.view", department=self.child)
        self.client.force_login(self.operator)

        response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["leaders"], [])
        self.assertEqual(
            self.client.get(url, {"project_id": self.outside.pk}).status_code, 404
        )

        grant(self.org, self.operator_membership, "projects.manage_lead", department=self.child)
        self.assertEqual(self.client.get(url).json()["leaders"], [])
        response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {row["id"] for row in response.json()["leaders"]},
            {self.org.memberships.get(user=self.owner).pk,
             self.operator_membership.pk, self.lead_membership.pk},
        )
        self.assertNotIn(
            self.archived_department.pk, {row["id"] for row in response.json()["departments"]}
        )
        updated = write(
            self.client, "put", self.project_url(self.local, "lead/"),
            {"lead_membership_id": self.lead_membership.pk},
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(
            updated.json()["lead"], {"id": self.lead_membership.pk, "username": self.lead.username}
        )

    def test_related_ids_must_belong_to_tenant_and_be_active_for_new_links(self):
        self.client.force_login(self.owner)
        for department, expected in ((self.foreign_department, 404), (self.archived_department, 409)):
            with self.subTest(department=department.pk):
                self.assertEqual(
                    write(
                        self.client, "put", self.project_url(self.local, "responsible-department/"),
                        {"responsible_department_id": department.pk},
                    ).status_code,
                    expected,
                )
                self.assertEqual(
                    write(
                        self.client, "put", self.project_url(self.local, f"departments/{department.pk}/"),
                    ).status_code,
                    expected,
                )
        for membership, expected in ((self.foreign_membership, 404), (self.inactive_membership, 409)):
            with self.subTest(membership=membership.pk):
                self.assertEqual(
                    write(
                        self.client, "put", self.project_url(self.local, "lead/"),
                        {"lead_membership_id": membership.pk},
                    ).status_code,
                    expected,
                )
        self.local.refresh_from_db()
        self.assertEqual(self.local.responsible_department_id, self.child.pk)
        self.assertIsNone(self.local.lead_membership_id)
        self.assertFalse(self.local.department_links.exists())

    def test_archived_project_is_immutable_and_keeps_history(self):
        ProjectDepartmentLink.objects.create(
            organization=self.org, project=self.outside, department=self.root
        )
        self.client.force_login(self.owner)
        url = self.project_url(self.outside)
        self.assertEqual(write(self.client, "delete", url).status_code, 204)
        for method, target_url, payload in (
            ("patch", url, {"name": "No change"}),
            ("put", f"{url}lead/", {"lead_membership_id": None}),
            ("put", f"{url}responsible-department/", {"responsible_department_id": None}),
            ("put", f"{url}departments/{self.child.pk}/", None),
            ("delete", f"{url}departments/{self.root.pk}/", None),
        ):
            with self.subTest(method=method, target_url=target_url):
                self.assertEqual(write(self.client, method, target_url, payload).status_code, 409)
        self.outside.refresh_from_db()
        self.assertTrue(self.outside.is_archived)
        self.assertEqual(self.outside.lead_membership_id, self.lead_membership.pk)
        self.assertEqual(self.outside.responsible_department_id, self.sibling.pk)
        self.assertEqual(self.outside.department_links.get().department_id, self.root.pk)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_archived_department_disables_branch_visibility_without_erasing_history(self):
        grant(self.org, self.operator_membership, "projects.view", department=self.root, descendants=True)
        ProjectDepartmentLink.objects.create(
            organization=self.org, project=self.local, department=self.child
        )
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(self.project_url(self.local)).status_code, 200)

        self.client.force_login(self.owner)
        department_url = f"{self.org_url}departments/{self.child.pk}/"
        self.assertEqual(write(self.client, "delete", department_url).status_code, 204)
        response = self.client.get(self.project_url(self.local))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["responsible_department"],
            {"id": self.child.pk, "name": self.child.name, "is_archived": True},
        )
        self.assertEqual(self.local.department_links.get().department_id, self.child.pk)

        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(self.project_url(self.local)).status_code, 404)

    def test_org_membership_deactivation_clears_only_nonarchived_project_lead(self):
        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "delete", self.project_url(self.outside)).status_code, 204)
        lead_url = self.project_url(self.local, "lead/")
        response = write(
            self.client, "put", lead_url, {"lead_membership_id": self.lead_membership.pk}
        )
        self.assertEqual(response.status_code, 200, response.content)

        OrganizationMembership.objects.filter(pk=self.lead_membership.pk).update(is_active=False)

        self.local.refresh_from_db()
        self.outside.refresh_from_db()
        self.assertIsNone(self.local.lead_membership_id)
        self.assertEqual(self.outside.lead_membership_id, self.lead_membership.pk)

    def test_postgres_rejects_cross_tenant_relationships_and_invalid_dates(self):
        cases = [
            Project(
                organization=self.org, created_by=self.owner, name="Invalid foreign department",
                responsible_department=self.foreign_department,
            ),
            Project(
                organization=self.org, created_by=self.owner, name="Invalid foreign lead",
                lead_membership=self.foreign_membership,
            ),
            Project(
                organization=self.org, created_by=self.owner, name="Invalid inactive lead",
                lead_membership=self.inactive_membership,
            ),
            Project(
                organization=self.org, created_by=self.owner, name="Invalid archived department",
                responsible_department=self.archived_department,
            ),
            Project(
                organization=self.org, created_by=self.owner, name="Invalid date",
                start_date="2026-11-12", planned_end_date="2026-11-11",
            ),
            ProjectDepartmentLink(
                organization=self.org, project=self.local, department=self.foreign_department,
            ),
            ProjectDepartmentLink(
                organization=self.org, project=self.foreign_project, department=self.child,
            ),
            ProjectDepartmentLink(
                organization=self.org, project=self.local, department=self.archived_department,
            ),
        ]
        for row in cases:
            with self.subTest(row=type(row).__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    type(row).objects.bulk_create([row])
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class ProjectConcurrencyTests(TransactionTestCase):
    def test_concurrent_responsibility_changes_recheck_scope_under_lock(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="project-race-owner")
        organization = create_organization(user=owner, name="Project race workspace")
        origin = Department.objects.create(organization=organization, name="Origin")
        project = Project.objects.create(
            organization=organization, created_by=owner, name="Race project",
            responsible_department=origin,
        )
        candidates = []
        for name in ("first", "second"):
            operator = users.create_user(username=f"project-race-{name}")
            membership = OrganizationMembership.objects.create(
                organization=organization, user=operator
            )
            destination = Department.objects.create(organization=organization, name=name)
            for department in (origin, destination):
                grant(
                    organization, membership, "projects.view", "projects.manage_departments",
                    department=department,
                )
            candidates.append((operator, destination))
        start = Barrier(2, timeout=5)

        def move(operator, destination):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    set_responsible_department(
                        user=operator, org_id=organization.pk, project_id=project.pk,
                        responsible_department_id=destination.pk,
                    )
            except (PermissionDenied, Http404):
                return None
            finally:
                worker_connection.close()
            return destination.pk

        with ThreadPoolExecutor(max_workers=2) as pool:
            moves = [pool.submit(move, *candidate) for candidate in candidates]
            results = [future.result(timeout=12) for future in moves]
        self.assertEqual(results.count(None), 1)
        project.refresh_from_db()
        self.assertEqual(project.responsible_department_id, next(pk for pk in results if pk is not None))
