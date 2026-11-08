from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.access.models import RolePermission
from apps.departments.models import Department, DepartmentMembership
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership, StaffingAllocation, StaffingRequest
from apps.projects.staffing import StaffingConflict, allocate
from tests.test_department_memberships import write
from tests.test_projects import grant


API = "/api/v1/organizations/"
INVITATIONS = f"{API}project-invitations/"


class StaffingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="staffing-owner")
        cls.foreign_owner = users.create_superuser(username="staffing-foreign-owner")
        cls.lead = users.create_user(username="staffing-lead")
        cls.allocator = users.create_user(username="staffing-allocator")
        cls.first = users.create_user(username="staffing-first")
        cls.second = users.create_user(username="staffing-second")
        cls.other = users.create_user(username="staffing-other")
        cls.org = create_organization(user=cls.owner, name="Staffing workspace")
        cls.foreign_org = create_organization(user=cls.foreign_owner, name="Foreign staffing workspace")

        def add_member(user):
            return OrganizationMembership.objects.create(organization=cls.org, user=user)

        cls.lead_member = add_member(cls.lead)
        cls.allocator_member = add_member(cls.allocator)
        cls.first_member = add_member(cls.first)
        cls.second_member = add_member(cls.second)
        cls.other_member = add_member(cls.other)
        cls.foreign_member = cls.foreign_org.memberships.get(user=cls.foreign_owner)

        cls.target = Department.objects.create(organization=cls.org, name="Specialist department")
        cls.subdepartment = Department.objects.create(
            organization=cls.org, name="Specialist branch", parent=cls.target
        )
        cls.other_department = Department.objects.create(organization=cls.org, name="Other department")
        cls.foreign_department = Department.objects.create(
            organization=cls.foreign_org, name="Foreign specialist department"
        )
        cls.first_department_member = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.target, organization_membership=cls.first_member
        )
        cls.second_department_member = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.subdepartment, organization_membership=cls.second_member
        )
        cls.other_department_member = DepartmentMembership.objects.create(
            organization=cls.org, department=cls.other_department, organization_membership=cls.other_member
        )
        cls.foreign_department_member = DepartmentMembership.objects.create(
            organization=cls.foreign_org, department=cls.foreign_department,
            organization_membership=cls.foreign_member,
        )
        cls.project = Project.objects.create(
            organization=cls.org, name="Pilot staffing project", created_by=cls.owner,
            lead_membership=cls.lead_member, status=Project.Status.ACTIVE,
        )
        cls.other_project = Project.objects.create(
            organization=cls.org, name="Different staffing project", created_by=cls.owner,
            status=Project.Status.ACTIVE,
        )
        cls.foreign_project = Project.objects.create(
            organization=cls.foreign_org, name="Foreign staffing project", created_by=cls.foreign_owner,
            status=Project.Status.ACTIVE,
        )
        cls.allocator_grant = grant(
            cls.org, cls.allocator_member, "departments.staffing.allocate",
            department=cls.target, descendants=True,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org_url = f"{API}{self.org.pk}/"
        self.project_url = f"{self.org_url}projects/{self.project.pk}/"
        self.drafts = f"{self.project_url}staffing-requests/"
        self.queue = f"{self.org_url}staffing-requests/"
        self.team_url = f"{self.project_url}team/"

    def request_url(self, staffing_request, suffix=""):
        return f"{self.queue}{staffing_request.pk}/{suffix}"

    def request_fixture(self, *, count=2, status=StaffingRequest.Status.SENT, department=None):
        return StaffingRequest.objects.create(
            organization=self.org, project=self.project, department=department or self.target,
            created_by=self.owner, requested_count=count, description="Help with pilot",
            status=status, submitted_at=None if status == StaffingRequest.Status.DRAFT else timezone.now(),
        )

    def allocate(self, staffing_request, *members, csrf=True):
        return write(
            self.client, "post", self.request_url(staffing_request, "allocate/"),
            {"department_membership_ids": [member.pk for member in members]},
            csrf=csrf,
        )

    def respond(self, allocation, decision, *, csrf=True):
        return write(
            self.client, "post", f"{INVITATIONS}{allocation.pk}/respond/",
            {"decision": decision}, csrf=csrf,
        )

    def invitation_mode(self):
        Organization.objects.filter(pk=self.org.pk).update(
            project_assignment_mode=Organization.ProjectAssignmentMode.INVITATION
        )

    def draft_payload(self, **updates):
        return {
            "department_id": self.target.pk,
            "requested_count": 2,
            "description": "Review pilot materials",
            "specialization": "Editing",
            "desired_start": "2026-11-10",
            "duration_days": 5,
            **updates,
        }

    def test_owner_creates_edits_and_submits_draft_to_the_department_queue(self):
        self.client.force_login(self.owner)
        response = write(self.client, "post", self.drafts, self.draft_payload())
        self.assertEqual(response.status_code, 201, response.content)
        staffing_request = StaffingRequest.objects.get(pk=response.json()["id"])
        self.assertEqual(staffing_request.status, StaffingRequest.Status.DRAFT)
        self.assertEqual(staffing_request.department_id, self.target.pk)
        self.assertEqual(staffing_request.project_id, self.project.pk)
        self.assertEqual(staffing_request.created_by_id, self.owner.pk)

        response = write(
            self.client, "patch", self.request_url(staffing_request),
            {"requested_count": 3, "description": "Updated draft"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.requested_count, 3)
        self.assertEqual(staffing_request.description, "Updated draft")

        self.client.force_login(self.allocator)
        queue = self.client.get(self.queue)
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertNotIn(staffing_request.pk, {row["id"] for row in queue.json()})
        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 404)

        self.client.force_login(self.owner)
        response = write(self.client, "post", self.request_url(staffing_request, "submit/"))
        self.assertEqual(response.status_code, 200, response.content)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.SENT)
        self.assertEqual(
            write(self.client, "patch", self.request_url(staffing_request), {"requested_count": 1})
            .status_code,
            200,
        )
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.requested_count, 1)
        self.assertEqual(
            write(self.client, "patch", self.request_url(staffing_request), {"specialization": "x" * 201})
            .status_code,
            422,
        )
        self.client.force_login(self.allocator)
        self.assertIn(staffing_request.pk, {row["id"] for row in self.client.get(self.queue).json()})

    def test_staffing_requests_require_ownership_or_the_actual_lead_with_roles(self):
        self.client.force_login(self.lead)
        self.assertIn(
            write(self.client, "post", self.drafts, self.draft_payload()).status_code, {403, 404}
        )

        grant(self.org, self.lead_member, "projects.view", "projects.staffing.request")
        response = write(self.client, "post", self.drafts, self.draft_payload())
        self.assertEqual(response.status_code, 201, response.content)
        other_url = f"{self.org_url}projects/{self.other_project.pk}/staffing-requests/"
        self.assertEqual(write(self.client, "post", other_url, self.draft_payload()).status_code, 403)

        grant(self.org, self.other_member, "projects.view", "projects.staffing.request")
        self.client.force_login(self.other)
        self.assertEqual(write(self.client, "post", self.drafts, self.draft_payload()).status_code, 403)
        self.assertEqual(StaffingRequest.objects.count(), 1)

    def test_direct_allocations_fill_incrementally_and_only_grant_team_read_access(self):
        staffing_request = self.request_fixture()
        self.client.force_login(self.allocator)
        response = self.allocate(staffing_request, self.first_department_member)
        self.assertEqual(response.status_code, 200, response.content)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.PARTIAL)
        membership = ProjectMembership.objects.get(project=self.project, organization_membership=self.first_member)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.source_department_id, self.target.pk)

        response = self.allocate(staffing_request, self.second_department_member)
        self.assertEqual(response.status_code, 200, response.content)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.FULFILLED)
        self.assertEqual(
            set(staffing_request.allocations.values_list("status", flat=True)),
            {StaffingAllocation.Status.ACCEPTED},
        )
        self.assertEqual(self.project.memberships.filter(is_active=True).count(), 2)
        self.assertTrue(self.first_department_member.is_active)

        self.client.force_login(self.first)
        self.assertEqual(self.client.get(self.project_url).status_code, 200)
        self.assertEqual(self.client.get(self.team_url).status_code, 200)
        self.assertEqual(
            write(self.client, "patch", self.project_url, {"name": "Unauthorized rename"}).status_code,
            403,
        )
        self.assertEqual(
            write(self.client, "delete", f"{self.team_url}{membership.pk}/").status_code, 403
        )
        self.assertEqual(self.client.get(self.drafts).status_code, 403)
        self.assertEqual(
            self.client.get(f"{self.org_url}projects/{self.other_project.pk}/").status_code, 404
        )

        self.client.force_login(self.owner)
        self.assertEqual(
            write(self.client, "delete", f"{self.team_url}{membership.pk}/").status_code, 204
        )
        membership.refresh_from_db()
        self.first_department_member.refresh_from_db()
        self.assertFalse(membership.is_active)
        self.assertIsNotNone(membership.ended_at)
        self.assertTrue(self.first_department_member.is_active)
        self.client.force_login(self.first)
        self.assertEqual(self.client.get(self.project_url).status_code, 404)

    def test_team_view_and_manage_permissions_are_separate(self):
        membership = ProjectMembership.objects.create(
            organization=self.org, project=self.project, organization_membership=self.first_member,
        )
        self.client.force_login(self.other)
        grant(self.org, self.other_member, "projects.view")
        self.assertEqual(self.client.get(self.team_url).status_code, 403)

        grant(self.org, self.other_member, "projects.team.view")
        self.assertEqual(self.client.get(self.team_url).status_code, 200)
        member_url = f"{self.team_url}{membership.pk}/"
        self.assertEqual(write(self.client, "delete", member_url).status_code, 403)

        grant(self.org, self.other_member, "projects.team.manage")
        self.assertEqual(write(self.client, "delete", member_url).status_code, 204)
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)

    def test_pending_capacity_is_freed_by_decline_and_revoke_and_only_accept_adds_membership(self):
        self.invitation_mode()
        staffing_request = self.request_fixture(count=1)
        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 200)
        first_invitation = staffing_request.allocations.get()
        self.assertEqual(first_invitation.status, StaffingAllocation.Status.PENDING)
        self.assertEqual(first_invitation.mode, Organization.ProjectAssignmentMode.INVITATION)
        self.assertFalse(self.project.memberships.exists())
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 409)
        self.assertEqual(staffing_request.allocations.count(), 1)

        self.client.force_login(self.second)
        self.assertEqual(self.client.get(INVITATIONS).json(), [])
        self.assertEqual(self.respond(first_invitation, "accept").status_code, 404)
        self.client.force_login(self.first)
        self.assertEqual(
            {row["id"] for row in self.client.get(INVITATIONS).json()}, {first_invitation.pk}
        )
        self.assertEqual(self.respond(first_invitation, "decline").status_code, 200)
        first_invitation.refresh_from_db()
        self.assertEqual(first_invitation.status, StaffingAllocation.Status.DECLINED)

        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 200)
        second_invitation = staffing_request.allocations.get(status=StaffingAllocation.Status.PENDING)
        revoke_url = self.request_url(staffing_request, f"allocations/{second_invitation.pk}/")
        self.assertEqual(write(self.client, "delete", revoke_url).status_code, 200)
        second_invitation.refresh_from_db()
        self.assertEqual(second_invitation.status, StaffingAllocation.Status.REVOKED)
        self.assertFalse(self.project.memberships.exists())

        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 200)
        accepted = staffing_request.allocations.get(status=StaffingAllocation.Status.PENDING)
        self.client.force_login(self.first)
        self.assertEqual(self.respond(accepted, "accept", csrf=False).status_code, 403)
        self.assertEqual(self.respond(accepted, "accept").status_code, 200)
        self.assertEqual(self.respond(accepted, "accept").status_code, 409)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.FULFILLED)
        self.assertTrue(self.project.memberships.get(organization_membership=self.first_member).is_active)

    def test_excess_or_mixed_valid_and_invalid_allocations_do_not_partially_write(self):
        only_one = self.request_fixture(count=1)
        two_places = self.request_fixture(count=2)
        self.client.force_login(self.allocator)
        for staffing_request, members, status in (
            (only_one, (self.first_department_member, self.second_department_member), 409),
            (two_places, (self.first_department_member, self.other_department_member), 404),
            (two_places, (self.first_department_member, self.foreign_department_member), 404),
        ):
            with self.subTest(members=members):
                response = self.allocate(staffing_request, *members)
                self.assertEqual(response.status_code, status, response.content)
                self.assertFalse(staffing_request.allocations.exists())
                self.assertFalse(self.project.memberships.exists())
                staffing_request.refresh_from_db()
                self.assertEqual(staffing_request.status, StaffingRequest.Status.SENT)

    def test_multiple_department_paths_cannot_allocate_one_person_twice(self):
        staffing_request = self.request_fixture()
        same_person = DepartmentMembership.objects.create(
            organization=self.org, department=self.subdepartment,
            organization_membership=self.first_member,
        )
        self.client.force_login(self.allocator)
        response = self.allocate(staffing_request, self.first_department_member, same_person)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(staffing_request.allocations.exists())
        self.assertFalse(self.project.memberships.exists())

    def test_descendant_allocator_scope_limits_candidates_and_cross_tenant_access(self):
        staffing_request = self.request_fixture()
        RolePermission.objects.filter(role=self.allocator_grant.role).update(include_descendants=False)
        self.client.force_login(self.allocator)
        candidates = self.client.get(self.request_url(staffing_request, "candidates/"))
        self.assertEqual(candidates.status_code, 200, candidates.content)
        self.assertNotContains(candidates, self.second.username)
        self.assertNotContains(candidates, self.other.username)
        self.assertNotContains(candidates, self.foreign_owner.username)
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 404)
        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 200)

        inaccessible = self.request_fixture(department=self.other_department)
        self.assertEqual(self.client.get(self.request_url(inaccessible)).status_code, 404)
        self.assertEqual(self.allocate(inaccessible, self.other_department_member).status_code, 404)

        self.client.force_login(self.foreign_owner)
        self.assertEqual(self.client.get(self.queue).status_code, 404)
        self.assertEqual(self.client.get(self.request_url(staffing_request)).status_code, 404)

    def test_allocator_cannot_revoke_hidden_invitation_for_descendant_candidate(self):
        self.invitation_mode()
        staffing_request = self.request_fixture()
        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 200)
        invitation = staffing_request.allocations.get()
        RolePermission.objects.filter(role=self.allocator_grant.role).update(include_descendants=False)

        response = self.client.get(self.request_url(staffing_request))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["history"], [])
        url = self.request_url(staffing_request, f"allocations/{invitation.pk}/")
        self.assertEqual(write(self.client, "delete", url).status_code, 404)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, StaffingAllocation.Status.PENDING)

    def test_candidate_load_does_not_expose_inaccessible_projects(self):
        staffing_request = self.request_fixture()
        visible = Project.objects.create(
            organization=self.org, created_by=self.owner, name="Visible candidate assignment",
            responsible_department=self.target,
        )
        for project in (visible, self.other_project):
            ProjectMembership.objects.create(
                organization=self.org, project=project, organization_membership=self.first_member,
            )
        grant(self.org, self.allocator_member, "projects.view", department=self.target)
        self.client.force_login(self.allocator)

        response = self.client.get(self.request_url(staffing_request, "candidates/"))

        self.assertEqual(response.status_code, 200, response.content)
        candidates = {row["id"]: row for row in response.json()}
        self.assertEqual(candidates[self.first_department_member.pk]["projects"], [visible.name])
        self.assertNotContains(response, self.other_project.name)

    def test_cancellation_revokes_pending_allocation_but_preserves_accepted_team(self):
        staffing_request = self.request_fixture()
        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 200)
        self.invitation_mode()
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 200)
        invitation = staffing_request.allocations.get(status=StaffingAllocation.Status.PENDING)

        self.client.force_login(self.owner)
        self.assertEqual(
            write(self.client, "delete", self.request_url(staffing_request)).status_code, 200
        )
        staffing_request.refresh_from_db()
        invitation.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.CANCELLED)
        self.assertEqual(invitation.status, StaffingAllocation.Status.REVOKED)
        self.assertTrue(self.project.memberships.get(organization_membership=self.first_member).is_active)
        self.assertFalse(self.project.memberships.filter(organization_membership=self.second_member).exists())

        self.client.force_login(self.second)
        self.assertEqual(self.respond(invitation, "accept").status_code, 409)

    def test_department_rejection_records_comment_without_creating_team(self):
        staffing_request = self.request_fixture()
        self.client.force_login(self.allocator)
        response = write(
            self.client, "post", self.request_url(staffing_request, "reject/"),
            {"comment": "No availability"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.REJECTED)
        self.assertEqual(staffing_request.comment, "No availability")
        self.assertEqual(staffing_request.handled_by_id, self.allocator.pk)
        self.assertFalse(self.project.memberships.exists())

    def test_source_or_organization_membership_deactivation_closes_engagements(self):
        self.invitation_mode()
        staffing_request = self.request_fixture(count=1)
        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.first_department_member).status_code, 200)
        invitation = staffing_request.allocations.get()

        DepartmentMembership.objects.filter(pk=self.first_department_member.pk).update(
            is_active=False, ended_at=timezone.now(),
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, StaffingAllocation.Status.REVOKED)
        self.assertIsNotNone(invitation.resolved_at)
        self.assertFalse(self.project.memberships.exists())

        Organization.objects.filter(pk=self.org.pk).update(project_assignment_mode="direct")
        self.assertEqual(self.allocate(staffing_request, self.second_department_member).status_code, 200)
        membership = self.project.memberships.get(organization_membership=self.second_member)
        self.assertTrue(membership.is_active)

        OrganizationMembership.objects.filter(pk=self.second_member.pk).update(is_active=False)
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)
        self.assertIsNotNone(membership.ended_at)

    def test_staffing_mutations_require_session_csrf_and_owner_only_policy(self):
        staffing_request = self.request_fixture()
        self.assertEqual(self.client.get(self.team_url).status_code, 401)
        self.assertEqual(self.client.get(INVITATIONS).status_code, 401)
        self.client.force_login(self.allocator)
        self.assertEqual(self.allocate(staffing_request, self.first_department_member, csrf=False).status_code, 403)

        policy_url = f"{self.org_url}staffing-policy/"
        body = {"mode": "invitation"}
        self.assertEqual(write(self.client, "patch", policy_url, body).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(write(self.client, "patch", policy_url, body, csrf=False).status_code, 403)
        self.assertEqual(write(self.client, "patch", policy_url, body).status_code, 204)
        self.org.refresh_from_db()
        self.assertEqual(self.org.project_assignment_mode, Organization.ProjectAssignmentMode.INVITATION)

    def test_bulk_cross_tenant_or_mismatched_references_are_rejected(self):
        staffing_request = self.request_fixture()
        rows = (
            ProjectMembership(
                organization=self.org, project=self.project,
                organization_membership=self.foreign_member,
            ),
            ProjectMembership(
                organization=self.org, project=self.foreign_project,
                organization_membership=self.first_member,
            ),
            StaffingRequest(
                organization=self.org, project=self.project, department=self.foreign_department,
                created_by=self.owner, requested_count=1, description="Invalid",
            ),
            StaffingAllocation(
                organization=self.org, project=self.other_project, request=staffing_request,
                organization_membership=self.first_member,
                department_membership=self.first_department_member, allocated_by=self.allocator,
                mode=Organization.ProjectAssignmentMode.INVITATION,
            ),
            StaffingAllocation(
                organization=self.org, project=self.project, request=staffing_request,
                organization_membership=self.first_member,
                department_membership=self.second_department_member, allocated_by=self.allocator,
                mode=Organization.ProjectAssignmentMode.INVITATION,
            ),
        )
        for row in rows:
            with self.subTest(model=type(row).__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    type(row).objects.bulk_create([row])
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class StaffingConcurrencyTests(TransactionTestCase):
    def test_concurrent_allocations_for_one_place_do_not_overfill(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="staffing-race-owner")
        organization = create_organization(user=owner, name="Allocation race")
        department = Department.objects.create(organization=organization, name="Specialists")
        project = Project.objects.create(
            organization=organization, name="Race project", created_by=owner, status=Project.Status.ACTIVE
        )
        staffing_request = StaffingRequest.objects.create(
            organization=organization, project=project, department=department, created_by=owner,
            requested_count=1, description="Only one place", status=StaffingRequest.Status.SENT,
            submitted_at=timezone.now(),
        )
        candidates = []
        for name in ("first", "second"):
            user = users.create_user(username=f"staffing-race-{name}")
            member = OrganizationMembership.objects.create(organization=organization, user=user)
            candidates.append(
                DepartmentMembership.objects.create(
                    organization=organization, department=department, organization_membership=member
                )
            )

        start = Barrier(2, timeout=5)

        def allocate_candidate(candidate):
            worker_connection = connections["default"]
            try:
                with transaction.atomic():
                    with worker_connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    start.wait()
                    allocate(
                        user=owner, org_id=organization.pk, request_id=staffing_request.pk,
                        department_membership_ids=[candidate.pk],
                    )
            except StaffingConflict:
                return "conflict"
            finally:
                worker_connection.close()
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(allocate_candidate, candidate) for candidate in candidates]
            self.assertCountEqual(
                [future.result(timeout=12) for future in futures], ["success", "conflict"]
            )
        staffing_request.refresh_from_db()
        self.assertEqual(staffing_request.status, StaffingRequest.Status.FULFILLED)
        self.assertEqual(staffing_request.allocations.count(), 1)
        self.assertEqual(project.memberships.filter(is_active=True).count(), 1)
