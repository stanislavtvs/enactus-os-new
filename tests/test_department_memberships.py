from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.departments import membership_services
from apps.departments.models import Department, DepartmentInvitation, DepartmentMembership
from apps.departments.services import DepartmentConflict
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization


API = "/api/v1/organizations/"
INBOX = f"{API}department-invitations/"


def write(client, method, url, data=None, *, csrf=True):
    headers = {}
    if csrf:
        client.get(reverse("login"))
        headers["X-CSRFToken"] = client.cookies[settings.CSRF_COOKIE_NAME].value
    if data is None:
        return getattr(client, method)(url, headers=headers)
    return getattr(client, method)(url, data, content_type="application/json", headers=headers)


class DepartmentMembershipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="directory-owner")
        cls.foreign_owner = users.create_superuser(username="directory-foreign-owner")
        cls.manager = users.create_user(username="directory-manager")
        cls.viewer = users.create_user(username="directory-viewer")
        cls.target = users.create_user(username="directory-target")
        cls.unassigned = users.create_user(username="directory-unassigned")
        cls.inactive = users.create_user(username="directory-inactive")
        cls.org = create_organization(user=cls.owner, name="Directory workspace")
        cls.other_org = create_organization(
            user=cls.foreign_owner, name="Foreign directory workspace"
        )
        cls.foreign_org_membership = OrganizationMembership.objects.get(
            organization=cls.other_org, user=cls.foreign_owner
        )
        cls.manager_org_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.manager, can_manage_members=True
        )
        cls.viewer_org_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.viewer
        )
        cls.target_org_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.target
        )
        cls.unassigned_org_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.unassigned
        )
        cls.inactive_org_membership = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.inactive, is_active=False
        )

        cls.root = Department.objects.create(organization=cls.org, name="Root")
        cls.child = Department.objects.create(
            organization=cls.org, parent=cls.root, name="Child"
        )
        cls.leaf = Department.objects.create(
            organization=cls.org, parent=cls.child, name="Leaf"
        )
        cls.outside = Department.objects.create(organization=cls.org, name="Outside branch")
        cls.other_department = Department.objects.create(
            organization=cls.other_org, name="Foreign department"
        )
        cls.viewer_leaf = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.leaf,
            organization_membership=cls.viewer_org_membership,
        )
        cls.target_direct = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.root,
            organization_membership=cls.target_org_membership,
        )
        cls.target_inherited = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.child,
            organization_membership=cls.target_org_membership,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def department_url(self, department):
        return f"{API}{self.org.pk}/departments/{department.pk}/"

    def members(self, department, scope):
        return self.client.get(f"{self.department_url(department)}members/", {"scope": scope})

    def issue(self, department, target=None, *, csrf=True):
        target = target or self.target_org_membership
        return write(
            self.client,
            "post",
            f"{self.department_url(department)}invitations/",
            {"organization_membership_id": target.pk},
            csrf=csrf,
        )

    def respond(self, invitation, decision="accept", *, csrf=True):
        return write(
            self.client,
            "post",
            f"{INBOX}{invitation.pk}/respond/",
            {"decision": decision},
            csrf=csrf,
        )

    def create_invitation(self, department, target=None):
        return DepartmentInvitation.objects.create(
            organization=self.org,
            department=department,
            organization_membership=target or self.target_org_membership,
            invited_by=self.owner,
        )

    def change_directory_policy(self, visibility, *, csrf=True):
        return write(
            self.client,
            "patch",
            f"{API}{self.org.pk}/department-directory-policy/",
            {"visibility": visibility},
            csrf=csrf,
        )

    def test_branch_deduplicates_users_with_every_path_and_marks_direct(self):
        self.client.force_login(self.viewer)
        response = self.members(self.root, "branch")
        self.assertEqual(response.status_code, 200)
        by_id = {row["organization_membership_id"]: row for row in response.json()}
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(set(by_id), {self.target_org_membership.pk, self.viewer_org_membership.pk})
        self.assertEqual(by_id[self.target_org_membership.pk]["username"], self.target.username)
        self.assertCountEqual(
            by_id[self.target_org_membership.pk]["memberships"],
            [
                {"id": self.target_direct.pk, "department_id": self.root.pk,
                 "path": ["Root"], "is_direct": True},
                {"id": self.target_inherited.pk, "department_id": self.child.pk,
                 "path": ["Root", "Child"], "is_direct": False},
            ],
        )
        self.assertEqual(
            by_id[self.viewer_org_membership.pk]["memberships"],
            [
                {"id": self.viewer_leaf.pk, "department_id": self.leaf.pk,
                 "path": ["Root", "Child", "Leaf"], "is_direct": False},
            ],
        )

        direct = self.members(self.root, "direct")
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(
            [row["organization_membership_id"] for row in direct.json()],
            [self.target_org_membership.pk],
        )
        others = self.members(self.root, "others")
        self.assertEqual(others.status_code, 200)
        self.assertEqual(others.json(), [])

    def test_directory_scope_respects_branch_and_organization_policy(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.members(self.outside, "branch").status_code, 404)
        self.client.force_login(self.unassigned)
        self.assertEqual(self.members(self.root, "branch").status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.members(self.root, "branch").status_code, 200)
        self.assertEqual(self.members(self.root, "others").status_code, 200)
        self.assertEqual(self.change_directory_policy("organization").status_code, 403)

        self.client.force_login(self.owner)
        self.assertEqual(self.change_directory_policy("organization", csrf=False).status_code, 403)
        self.assertEqual(self.change_directory_policy("organization").status_code, 204)
        self.client.force_login(self.unassigned)
        self.assertEqual(self.members(self.root, "branch").status_code, 200)
        others = self.members(self.root, "others")
        self.assertEqual(others.status_code, 200)
        self.assertIn(self.unassigned.username, {row["username"] for row in others.json()})
        self.assertNotIn(self.inactive.username, {row["username"] for row in others.json()})
        self.assertNotIn(self.foreign_owner.username, {row["username"] for row in others.json()})
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.members(self.root, "branch").status_code, 404)

    def test_branch_directory_query_count_does_not_grow_with_member_count(self):
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connection) as baseline:
            self.assertEqual(self.members(self.root, "branch").status_code, 200)

        for index in range(8):
            user = get_user_model().objects.create_user(username=f"directory-extra-{index}")
            org_membership = OrganizationMembership.objects.create(organization=self.org, user=user)
            DepartmentMembership.objects.create(
                organization=self.org, department=self.leaf, organization_membership=org_membership
            )

        with CaptureQueriesContext(connection) as populated:
            response = self.members(self.root, "branch")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 10)
        self.assertEqual(len(populated), len(baseline))

    def test_managers_issue_invitations_but_cannot_cross_tenants_or_bypass_csrf(self):
        invite_url = f"{self.department_url(self.outside)}invitations/"
        self.assertEqual(self.client.get(invite_url).status_code, 401)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(invite_url).status_code, 403)
        self.assertEqual(self.issue(self.outside).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.issue(self.outside, csrf=False).status_code, 403)
        self.assertEqual(self.issue(self.outside, self.foreign_org_membership).status_code, 404)
        self.assertEqual(self.issue(self.other_department).status_code, 404)
        self.assertEqual(self.issue(self.outside, self.inactive_org_membership).status_code, 404)
        self.assertFalse(DepartmentInvitation.objects.exists())

        response = self.issue(self.outside)
        self.assertEqual(response.status_code, 201, response.content)
        invitation = DepartmentInvitation.objects.get(pk=response.json()["id"])
        self.assertEqual(invitation.invited_by, self.manager)
        self.assertEqual(invitation.department, self.outside)
        self.assertEqual(invitation.organization_membership, self.target_org_membership)
        self.assertEqual(self.issue(self.outside).status_code, 409)

    def test_invitation_response_requires_recipient_and_accepts_once(self):
        invitation = self.create_invitation(self.outside)
        self.assertEqual(self.client.get(INBOX).status_code, 401)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(INBOX).json(), [])
        self.assertEqual(self.respond(invitation).status_code, 404)
        self.client.force_login(self.target)
        self.assertEqual(
            [row["id"] for row in self.client.get(INBOX).json()], [invitation.pk]
        )
        self.assertEqual(self.respond(invitation, csrf=False).status_code, 403)
        self.assertFalse(self.outside.memberships.exists())

        response = self.respond(invitation)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "accepted")
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, DepartmentInvitation.Status.ACCEPTED)
        self.assertTrue(self.outside.memberships.get().is_active)
        self.assertEqual(self.respond(invitation).status_code, 409)
        self.assertEqual(self.outside.memberships.count(), 1)
        self.assertEqual(self.target_org_membership.department_memberships.count(), 3)

    def test_declining_and_revoking_do_not_create_membership(self):
        invitation = self.create_invitation(self.outside)
        self.client.force_login(self.target)
        self.assertEqual(self.respond(invitation, "decline").status_code, 200)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, DepartmentInvitation.Status.DECLINED)

        pending = self.create_invitation(self.outside)
        revoke_url = f"{self.department_url(self.outside)}invitations/{pending.pk}/"
        self.assertEqual(write(self.client, "delete", revoke_url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "delete", revoke_url).status_code, 204)
        pending.refresh_from_db()
        self.assertEqual(pending.status, DepartmentInvitation.Status.REVOKED)
        self.assertFalse(self.outside.memberships.exists())

    def test_member_removal_is_scoped_and_preserves_history_and_other_memberships(self):
        url = f"{self.department_url(self.root)}members/{self.target_direct.pk}/"
        wrong_department = f"{self.department_url(self.child)}members/{self.target_direct.pk}/"
        self.client.force_login(self.viewer)
        self.assertEqual(write(self.client, "delete", url).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(write(self.client, "delete", wrong_department).status_code, 404)
        self.assertEqual(write(self.client, "delete", url, csrf=False).status_code, 403)
        self.assertEqual(write(self.client, "delete", url).status_code, 204)
        self.target_direct.refresh_from_db()
        self.target_inherited.refresh_from_db()
        self.target_org_membership.refresh_from_db()
        self.assertFalse(self.target_direct.is_active)
        self.assertIsNotNone(self.target_direct.ended_at)
        self.assertTrue(self.target_inherited.is_active)
        self.assertTrue(self.target_org_membership.is_active)
        self.assertEqual(self.members(self.root, "direct").json(), [])

    def test_new_invitation_reactivates_the_existing_membership(self):
        self.client.force_login(self.owner)
        url = f"{self.department_url(self.root)}members/{self.target_direct.pk}/"
        self.assertEqual(write(self.client, "delete", url).status_code, 204)
        response = self.issue(self.root)
        self.assertEqual(response.status_code, 201, response.content)
        invitation = DepartmentInvitation.objects.get(pk=response.json()["id"])

        self.client.force_login(self.target)
        self.assertEqual(self.respond(invitation).status_code, 200)

        self.target_direct.refresh_from_db()
        self.assertTrue(self.target_direct.is_active)
        self.assertIsNone(self.target_direct.ended_at)
        self.assertEqual(
            self.target_org_membership.department_memberships.filter(
                department=self.root
            ).count(),
            1,
        )

    def test_deactivation_removes_directory_access_and_preserves_revoked_history(self):
        invitation = self.create_invitation(self.outside)
        OrganizationMembership.objects.filter(pk=self.target_org_membership.pk).update(
            is_active=False
        )

        self.target_direct.refresh_from_db()
        self.target_inherited.refresh_from_db()
        invitation.refresh_from_db()
        self.assertFalse(self.target_direct.is_active)
        self.assertIsNotNone(self.target_direct.ended_at)
        self.assertFalse(self.target_inherited.is_active)
        self.assertEqual(invitation.status, DepartmentInvitation.Status.REVOKED)
        self.assertIsNotNone(invitation.resolved_at)
        self.client.force_login(self.target)
        self.assertEqual(self.members(self.root, "branch").status_code, 404)
        self.assertEqual(self.client.get(INBOX).json(), [])
        self.assertEqual(self.respond(invitation).status_code, 404)
        self.assertFalse(self.outside.memberships.exists())
        self.client.force_login(self.owner)
        self.assertNotIn(
            self.target.username, {row["username"] for row in self.members(self.root, "branch").json()}
        )

    def test_archiving_subtree_ends_its_memberships_and_revokes_its_pending_invitations(self):
        invitation = self.create_invitation(self.leaf)
        self.client.force_login(self.owner)

        response = write(self.client, "delete", self.department_url(self.child))

        self.assertEqual(response.status_code, 204)
        self.target_direct.refresh_from_db()
        self.target_inherited.refresh_from_db()
        self.viewer_leaf.refresh_from_db()
        invitation.refresh_from_db()
        self.assertTrue(self.target_direct.is_active)
        self.assertFalse(self.target_inherited.is_active)
        self.assertIsNotNone(self.target_inherited.ended_at)
        self.assertFalse(self.viewer_leaf.is_active)
        self.assertIsNotNone(self.viewer_leaf.ended_at)
        self.assertEqual(invitation.status, DepartmentInvitation.Status.REVOKED)
        self.assertIsNotNone(invitation.resolved_at)
        self.assertEqual(self.members(self.leaf, "branch").status_code, 404)

        self.client.force_login(self.target)
        self.assertEqual(
            [(row["id"], row["status"]) for row in self.client.get(INBOX).json()],
            [(invitation.pk, "revoked")],
        )

    def flush_deferred_constraints(self):
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_database_rejects_bulk_cross_organization_membership_and_invitation(self):
        for model in (DepartmentMembership, DepartmentInvitation):
            for department, membership in (
                (self.outside, self.foreign_org_membership),
                (self.other_department, self.target_org_membership),
            ):
                with self.subTest(model=model.__name__, department=department.pk):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        model.objects.bulk_create(
                            [model(organization=self.org, department=department,
                                   organization_membership=membership)]
                        )
                        self.flush_deferred_constraints()

    def test_database_rejects_bulk_membership_and_invitation_for_inactive_members(self):
        for model in (DepartmentMembership, DepartmentInvitation):
            with self.subTest(model=model.__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.bulk_create(
                        [model(organization=self.org, department=self.outside,
                               organization_membership=self.inactive_org_membership)]
                    )
                    self.flush_deferred_constraints()

    def test_database_rejects_duplicate_membership_and_pending_invitation(self):
        self.create_invitation(self.outside)
        for model, department in (
            (DepartmentMembership, self.root),
            (DepartmentInvitation, self.outside),
        ):
            with self.subTest(model=model.__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.bulk_create(
                        [
                            model(
                                organization=self.org,
                                department=department,
                                organization_membership=self.target_org_membership,
                            )
                        ]
                    )
                    self.flush_deferred_constraints()

    def test_database_does_not_allow_changing_the_user_of_an_org_membership(self):
        replacement = get_user_model().objects.create_user(username="directory-replacement")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.filter(pk=self.target_org_membership.pk).update(
                user=replacement
            )
            self.flush_deferred_constraints()
        self.target_org_membership.refresh_from_db()
        self.assertEqual(self.target_org_membership.user_id, self.target.pk)


class DepartmentMembershipConcurrencyTests(TransactionTestCase):
    def concurrent_calls(self, service, **kwargs):
        start = Barrier(2, timeout=5)

        def worker():
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    service(**kwargs)
            except DepartmentConflict:
                return "conflict"
            finally:
                worker_connection.close()
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            second = pool.submit(worker)
            return [first.result(timeout=12), second.result(timeout=12)]

    def test_concurrent_invites_and_accepts_create_one_membership(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="department-invite-race-owner")
        target = users.create_user(username="department-invite-race-target")
        organization = create_organization(user=owner, name="Invite race workspace")
        department = Department.objects.create(organization=organization, name="Invite race")
        org_membership = OrganizationMembership.objects.create(
            organization=organization, user=target
        )
        results = self.concurrent_calls(
            membership_services.issue_invitation,
            user=owner,
            org_id=organization.pk,
            department_id=department.pk,
            organization_membership_id=org_membership.pk,
        )
        self.assertCountEqual(results, ["success", "conflict"])
        invitation = DepartmentInvitation.objects.get(department=department)

        results = self.concurrent_calls(
            membership_services.respond_to_invitation,
            user=target,
            invitation_id=invitation.pk,
            accept=True,
        )
        self.assertCountEqual(results, ["success", "conflict"])
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, DepartmentInvitation.Status.ACCEPTED)
        self.assertTrue(department.memberships.get().is_active)
