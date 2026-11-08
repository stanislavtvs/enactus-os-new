from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.access.models import PositionAssignment, Role, RoleAssignment, RolePermission
from apps.access.policy import Access, Permission
from apps.access.services import AccessConflict, assign_role, update_role
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from tests.test_department_memberships import write


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="access-owner")
        cls.foreign_owner = users.create_superuser(username="access-foreign-owner")
        cls.operator = users.create_user(username="access-operator")
        cls.target = users.create_user(username="access-target")
        cls.legacy = users.create_user(username="access-legacy-manager")
        cls.outsider = users.create_user(username="access-invite-recipient")
        cls.org = create_organization(user=cls.owner, name="Access workspace")
        cls.other_org = create_organization(user=cls.foreign_owner, name="Foreign workspace")
        cls.operator_org = OrganizationMembership.objects.create(organization=cls.org, user=cls.operator)
        cls.target_org = OrganizationMembership.objects.create(organization=cls.org, user=cls.target)
        cls.legacy_org = OrganizationMembership.objects.create(
            organization=cls.org, user=cls.legacy, can_manage_members=True
        )
        cls.foreign_membership = cls.other_org.memberships.get(user=cls.foreign_owner)
        cls.root = Department.objects.create(organization=cls.org, name="Root")
        cls.child = Department.objects.create(organization=cls.org, name="Child", parent=cls.root)
        cls.leaf = Department.objects.create(organization=cls.org, name="Leaf", parent=cls.child)
        cls.sibling = Department.objects.create(organization=cls.org, name="Sibling")
        cls.foreign_department = Department.objects.create(organization=cls.other_org, name="Foreign")
        cls.operator_root = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.root, organization_membership=cls.operator_org
        )
        cls.operator_child = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.child, organization_membership=cls.operator_org
        )
        cls.target_root = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.root, organization_membership=cls.target_org
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.api = f"/api/v1/organizations/{self.org.pk}/"

    def role(self, name, *permissions, organization=None):
        role = Role.objects.create(organization=organization or self.org, name=name)
        RolePermission.objects.bulk_create([
            RolePermission(role=role, code=code, include_descendants=descendants)
            for code, descendants in permissions
        ])
        return role

    def grant(self, role, member=None, department_member=None):
        return RoleAssignment.objects.create(
            organization=self.org,
            role=role,
            organization_membership=member or self.operator_org,
            department_membership=department_member,
        )

    def role_body(self, name, *permissions):
        return {
            "name": name,
            "permissions": [
                {"code": code, "include_descendants": descendants}
                for code, descendants in permissions
            ],
        }

    def department_url(self, department):
        return f"{self.api}departments/{department.pk}/"

    def assign(self, role, member, department_member=None):
        return write(
            self.client, "post", f"{self.api}role-assignments/",
            {
                "role_id": role.pk,
                "organization_membership_id": member.pk,
                "department_membership_id": department_member.pk if department_member else None,
            },
        )

    def test_scoped_roles_union_permissions_without_inheriting_other_actions(self):
        exact = self.role("Exact editor", (Permission.DEPARTMENTS_UPDATE, False))
        recursive = self.role("Subtree archivist", (Permission.DEPARTMENTS_ARCHIVE, True))
        move = self.role("Organization mover", (Permission.DEPARTMENTS_MOVE, False))
        self.grant(exact, department_member=self.operator_root)
        self.grant(recursive, department_member=self.operator_root)
        self.grant(move)

        access = Access(self.operator, self.org)
        self.assertTrue(access.allows(Permission.DEPARTMENTS_UPDATE, department=self.root))
        self.assertFalse(access.allows(Permission.DEPARTMENTS_UPDATE, department=self.child))
        self.assertFalse(access.allows(
            Permission.DEPARTMENTS_UPDATE, department=self.root, include_descendants=True
        ))
        self.assertTrue(access.allows(
            Permission.DEPARTMENTS_ARCHIVE, department=self.leaf, include_descendants=True
        ))
        self.assertFalse(access.allows(Permission.DEPARTMENTS_ARCHIVE, department=self.sibling))
        self.assertFalse(access.allows(Permission.DEPARTMENTS_ARCHIVE))
        self.assertTrue(access.allows(Permission.DEPARTMENTS_MOVE, department=self.sibling))
        self.assertFalse(access.allows(Permission.DEPARTMENTS_MOVE, department=self.foreign_department))

    def test_active_org_is_required_even_for_owner_or_platform_admin(self):
        self.assertTrue(Access(self.owner, self.org).allows(Permission.ROLES_MANAGE))
        self.assertFalse(Access(self.foreign_owner, self.org).allows(Permission.ROLES_MANAGE))
        self.assertFalse(Access(self.foreign_owner, self.org).allows(Permission.DEPARTMENTS_READ))
        self.assertTrue(Access(self.legacy, self.org).allows(Permission.DEPARTMENTS_UPDATE))
        self.assertFalse(Access(self.legacy, self.org).allows(Permission.ROLES_MANAGE))

        self.org.memberships.filter(user=self.owner).update(is_active=False)

        self.assertFalse(Access(self.owner, self.org).allows(Permission.ROLES_MANAGE))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"{self.api}roles/").status_code, 404)

    def test_deactivation_clears_legacy_management_even_after_bulk_reactivation(self):
        members = self.org.memberships.filter(pk=self.legacy_org.pk)
        members.update(is_active=False)
        members.update(is_active=True)
        self.assertFalse(Access(self.legacy, self.org).allows(Permission.DEPARTMENTS_UPDATE))
        self.assertFalse(Access(self.legacy, self.org).allows(Permission.ORGANIZATIONS_ONBOARDING_MANAGE))

    def test_access_page_exposes_administration_only_to_authorized_members(self):
        url = f"/organizations/{self.org.pk}/access/"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.target)
        page = self.client.get(url)
        self.assertContains(page, 'id="access-effective-list"')
        self.assertNotContains(page, 'id="access-role-form"')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), 'id="access-role-form"')

    def test_roles_api_requires_authentication_csrf_and_manage_permission(self):
        roles_url = f"{self.api}roles/"
        body = self.role_body("Scoped editor", (Permission.DEPARTMENTS_UPDATE, False))
        self.assertEqual(self.client.get(roles_url).status_code, 401)
        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(roles_url).status_code, 404)
        self.client.force_login(self.target)
        self.assertEqual(self.client.get(roles_url).status_code, 403)
        reader = self.role("Read roles", (Permission.ROLES_READ, False))
        self.grant(reader, member=self.target_org)
        self.assertEqual(self.client.get(roles_url).status_code, 200)
        self.assertEqual(write(self.client, "post", roles_url, body).status_code, 403)

        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "post", roles_url, body, csrf=False).status_code, 403)
        response = write(self.client, "post", roles_url, body)
        self.assertEqual(response.status_code, 201, response.content)
        role = Role.objects.get(pk=response.json()["id"])
        self.assertEqual(role.organization_id, self.org.pk)
        self.assertEqual(role.permissions.get().code, Permission.DEPARTMENTS_UPDATE)

        assignment = self.assign(role, self.target_org, self.target_root)
        self.assertEqual(assignment.status_code, 201, assignment.content)
        self.client.force_login(self.target)
        permissions = self.client.get(
            f"{self.api}permissions/effective/", {"department_id": self.root.pk}
        )
        self.assertEqual(permissions.status_code, 200)
        self.assertIn(Permission.DEPARTMENTS_UPDATE, permissions.json()["permissions"])
        child_permissions = self.client.get(
            f"{self.api}permissions/effective/", {"department_id": self.child.pk}
        )
        self.assertEqual(child_permissions.status_code, 200)
        self.assertNotIn(Permission.DEPARTMENTS_UPDATE, child_permissions.json()["permissions"])

        self.client.force_login(self.owner)
        revoke = write(
            self.client, "delete", f'{self.api}role-assignments/{assignment.json()["id"]}/'
        )
        self.assertEqual(revoke.status_code, 204)
        self.assertFalse(
            Access(self.target, self.org).allows(Permission.DEPARTMENTS_UPDATE, department=self.root)
        )

    def test_positions_are_independent_and_do_not_grant_permission(self):
        self.client.force_login(self.owner)
        position_url = f"{self.api}positions/"
        response = write(
            self.client, "post", position_url,
            {
                "title": Permission.ROLES_MANAGE,
                "organization_membership_id": self.target_org.pk,
                "department_membership_id": self.target_root.pk,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        position = PositionAssignment.objects.get(pk=response.json()["id"])
        self.assertFalse(Access(self.target, self.org).allows(Permission.ROLES_MANAGE))
        self.assertFalse(
            Access(self.target, self.org).allows(Permission.DEPARTMENTS_UPDATE, department=self.root)
        )
        self.assertEqual(write(self.client, "delete", f"{position_url}{position.pk}/").status_code, 204)
        position.refresh_from_db()
        self.assertIsNotNone(position.revoked_at)

    def test_delegated_role_manager_cannot_grant_role_control_or_exceed_scope(self):
        control = self.role("Manage access", (Permission.ROLES_MANAGE, False))
        exact = self.role("Exact editor", (Permission.DEPARTMENTS_UPDATE, False))
        equivalent = self.role("Other exact editor", (Permission.DEPARTMENTS_UPDATE, False))
        broad = self.role("Branch editor", (Permission.DEPARTMENTS_UPDATE, True))
        self.grant(control)
        self.grant(exact, department_member=self.operator_root)
        self.client.force_login(self.operator)

        self.assertEqual(self.assign(control, self.target_org).status_code, 403)
        self.assertEqual(
            self.assign(equivalent, self.operator_org, self.operator_root).status_code, 403
        )
        self.assertEqual(
            write(
                self.client,
                "post",
                f"{self.api}roles/",
                self.role_body("Escalated role", (Permission.ROLES_MANAGE, False)),
            ).status_code,
            403,
        )
        self.assertEqual(self.assign(broad, self.target_org, self.target_root).status_code, 403)
        self.assertEqual(self.assign(exact, self.target_org).status_code, 403)
        self.assertEqual(self.assign(exact, self.target_org, self.target_root).status_code, 201)
        self.assertEqual(RoleAssignment.objects.filter(organization_membership=self.target_org).count(), 1)

    def test_delegated_manager_cannot_edit_or_archive_roles_held_by_any_member(self):
        control = self.role("Manage access", (Permission.ROLES_MANAGE, False))
        assigned = self.role("Assigned role", (Permission.DEPARTMENTS_READ, False))
        self.grant(control)
        self.grant(assigned, member=self.target_org)
        self.client.force_login(self.operator)
        body = self.role_body("Changed role", (Permission.DEPARTMENTS_READ, False))
        for role in (control, assigned):
            with self.subTest(role=role.pk):
                url = f"{self.api}roles/{role.pk}/"
                self.assertEqual(write(self.client, "patch", url, body).status_code, 403)
                self.assertEqual(write(self.client, "delete", url).status_code, 403)
                role.refresh_from_db()
                self.assertFalse(role.is_archived)
                self.assertNotEqual(role.name, "Changed role")

    def test_role_assignments_hide_foreign_roles_and_unrelated_department_memberships(self):
        local = self.role("Local editor", (Permission.DEPARTMENTS_UPDATE, False))
        foreign = self.role("Foreign editor", organization=self.other_org)
        self.client.force_login(self.owner)

        self.assertEqual(self.assign(foreign, self.target_org).status_code, 404)
        self.assertEqual(self.assign(local, self.foreign_membership).status_code, 404)
        self.assertEqual(self.assign(local, self.target_org, self.operator_root).status_code, 404)
        self.assertFalse(RoleAssignment.objects.exists())

    def test_department_update_requires_its_grant_and_move_requires_both_scopes(self):
        source = self.role(
            "Source editor", (Permission.DEPARTMENTS_UPDATE, False),
            (Permission.DEPARTMENTS_MOVE, False),
        )
        self.grant(source, department_member=self.operator_child)
        self.client.force_login(self.operator)
        child_url = self.department_url(self.child)

        self.assertEqual(write(self.client, "patch", child_url, {"name": "Renamed"}).status_code, 200)
        self.assertEqual(
            write(self.client, "patch", self.department_url(self.sibling), {"name": "Denied"}).status_code,
            403,
        )
        self.assertEqual(
            write(self.client, "patch", child_url, {"parent_id": self.sibling.pk}).status_code, 403
        )
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.root.pk)

        target = DepartmentMembership.objects.create(
            organization=self.org, department=self.sibling, organization_membership=self.operator_org
        )
        target_mover = self.role("Target mover", (Permission.DEPARTMENTS_MOVE, False))
        self.grant(target_mover, department_member=target)
        response = write(self.client, "patch", child_url, {"parent_id": self.sibling.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.sibling.pk)

    def test_archiving_subtree_requires_permissions_on_its_descendants(self):
        role = self.role("Local archiver", (Permission.DEPARTMENTS_ARCHIVE, False))
        self.grant(role, department_member=self.operator_root)
        self.client.force_login(self.operator)
        url = self.department_url(self.root)

        self.assertEqual(write(self.client, "delete", url).status_code, 403)
        self.assertFalse(self.org.departments.filter(is_archived=True).exists())

        RolePermission.objects.filter(role=role).update(include_descendants=True)
        self.assertEqual(write(self.client, "delete", url).status_code, 204)
        self.assertEqual(
            set(self.org.departments.filter(is_archived=True).values_list("id", flat=True)),
            {self.root.pk, self.child.pk, self.leaf.pk},
        )

    def test_onboarding_permission_does_not_grant_owner_only_operations(self):
        members_url = f"{self.api}members/"
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(members_url).status_code, 403)
        role = self.role("Onboarding manager", (Permission.ORGANIZATIONS_ONBOARDING_MANAGE, False))
        self.grant(role)

        self.assertEqual(self.client.get(members_url).status_code, 200)
        self.assertEqual(
            write(
                self.client, "post", f"{self.api}invitations/",
                {"username": self.outsider.username},
            ).status_code,
            201,
        )
        self.assertEqual(
            write(self.client, "patch", f"{self.api}join-policy/", {"policy": "both"}).status_code,
            403,
        )

    def test_invitation_permission_is_scoped_and_does_not_reveal_hidden_candidates(self):
        role = self.role("Invitation operator", (Permission.DEPARTMENTS_INVITATIONS_MANAGE, False))
        self.grant(role, department_member=self.operator_child)
        self.client.force_login(self.operator)
        branch_url = f"{self.department_url(self.child)}invitations/"
        payload = {"organization_membership_id": self.target_org.pk}

        self.assertEqual(
            write(
                self.client, "post", f"{self.department_url(self.root)}invitations/", payload,
            ).status_code,
            403,
        )
        self.assertEqual(write(self.client, "post", branch_url, payload).status_code, 404)

        target_leaf = DepartmentMembership.objects.create(
            organization=self.org, department=self.leaf, organization_membership=self.target_org
        )
        self.assertEqual(write(self.client, "post", branch_url, payload).status_code, 201)
        self.assertEqual(
            write(
                self.client, "delete", f"{self.department_url(self.leaf)}members/{target_leaf.pk}/",
            ).status_code,
            403,
        )

    def test_role_assignment_never_revives_with_department_membership(self):
        role = self.role("Department editor", (Permission.DEPARTMENTS_UPDATE, False))
        assignment = self.grant(role, department_member=self.operator_root)
        self.assertTrue(
            Access(self.operator, self.org).allows(Permission.DEPARTMENTS_UPDATE, department=self.root)
        )

        DepartmentMembership.objects.filter(pk=self.operator_root.pk).update(
            is_active=False, ended_at=timezone.now()
        )
        DepartmentMembership.objects.filter(pk=self.operator_root.pk).update(
            is_active=True, ended_at=None
        )

        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.revoked_at)
        self.assertFalse(
            Access(self.operator, self.org).allows(Permission.DEPARTMENTS_UPDATE, department=self.root)
        )

    def test_role_and_position_never_revive_with_organization_membership(self):
        role = self.role("Organization editor", (Permission.DEPARTMENTS_UPDATE, False))
        assignment = self.grant(role)
        position = PositionAssignment.objects.create(
            organization=self.org, organization_membership=self.operator_org, title="Coordinator"
        )
        self.assertTrue(Access(self.operator, self.org).allows(Permission.DEPARTMENTS_UPDATE))

        OrganizationMembership.objects.filter(pk=self.operator_org.pk).update(is_active=False)
        OrganizationMembership.objects.filter(pk=self.operator_org.pk).update(is_active=True)

        assignment.refresh_from_db()
        position.refresh_from_db()
        self.assertIsNotNone(assignment.revoked_at)
        self.assertIsNotNone(position.revoked_at)
        self.assertFalse(Access(self.operator, self.org).allows(Permission.DEPARTMENTS_UPDATE))

    def test_postgresql_rejects_bulk_cross_tenant_role_and_position_assignments(self):
        local = self.role("Local editor")
        foreign = self.role("Foreign editor", organization=self.other_org)
        cases = [
            RoleAssignment(
                organization=self.org, role=foreign, organization_membership=self.target_org,
            ),
            RoleAssignment(
                organization=self.org, role=local, organization_membership=self.foreign_membership,
            ),
            RoleAssignment(
                organization=self.org, role=local, organization_membership=self.target_org,
                department_membership=self.operator_root,
            ),
            PositionAssignment(
                organization=self.org, organization_membership=self.foreign_membership,
                title="Foreign position",
            ),
            PositionAssignment(
                organization=self.org, organization_membership=self.target_org,
                department_membership=self.operator_root, title="Unrelated position",
            ),
        ]
        for assignment in cases:
            with self.subTest(assignment=type(assignment).__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    type(assignment).objects.bulk_create([assignment])
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class ConcurrentAccessTests(TransactionTestCase):
    def test_assignment_blocks_a_concurrent_delegated_edit_of_the_role(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="role-edit-owner")
        delegate = users.create_user(username="role-edit-delegate")
        target = users.create_user(username="role-edit-target")
        organization = create_organization(user=owner, name="Concurrent role edit")
        delegate_member = organization.memberships.create(user=delegate)
        target_member = organization.memberships.create(user=target)
        manager = organization.roles.create(name="Delegated manager")
        RolePermission.objects.bulk_create([
            RolePermission(role=manager, code=Permission.ROLES_MANAGE),
            RolePermission(role=manager, code=Permission.DEPARTMENTS_UPDATE),
        ])
        organization.role_assignments.create(role=manager, organization_membership=delegate_member)
        role = organization.roles.create(name="Editor")
        RolePermission.objects.create(role=role, code=Permission.DEPARTMENTS_UPDATE)
        assigned = Barrier(2, timeout=5)

        def assign():
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    assign_role(
                        user=owner, org_id=organization.pk, role_id=role.pk,
                        organization_membership_id=target_member.pk,
                    )
                    assigned.wait()
            finally:
                worker_connection.close()

        def edit():
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    assigned.wait()
                    update_role(
                        user=delegate, org_id=organization.pk, role_id=role.pk,
                        changes={"name": "Changed"},
                    )
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assignment = pool.submit(assign)
            edit_attempt = pool.submit(edit)
            assignment.result(timeout=12)
            with self.assertRaises(PermissionDenied):
                edit_attempt.result(timeout=12)
        role.refresh_from_db()
        self.assertEqual(role.name, "Editor")
        self.assertTrue(role.assignments.filter(organization_membership=target_member).exists())

    def test_concurrent_duplicate_role_assignments_create_one_active_grant(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="role-race-owner")
        target = users.create_user(username="role-race-target")
        organization = create_organization(user=owner, name="Role assignment race")
        membership = OrganizationMembership.objects.create(organization=organization, user=target)
        role = Role.objects.create(organization=organization, name="Editor")
        RolePermission.objects.create(role=role, code=Permission.DEPARTMENTS_UPDATE)
        start = Barrier(2, timeout=5)

        def worker():
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    assign_role(
                        user=owner, org_id=organization.pk, role_id=role.pk,
                        organization_membership_id=membership.pk,
                    )
            except AccessConflict:
                return "conflict"
            finally:
                worker_connection.close()
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            second = pool.submit(worker)
            self.assertCountEqual(
                [first.result(timeout=12), second.result(timeout=12)], ["success", "conflict"]
            )
        self.assertEqual(role.assignments.filter(revoked_at__isnull=True).count(), 1)
