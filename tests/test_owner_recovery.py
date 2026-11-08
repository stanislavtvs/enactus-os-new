from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.organizations.models import Organization, OrganizationMembership, OwnershipTransfer
from apps.organizations.ownership import offer_ownership
from apps.organizations.platform_models import PlatformAuditEvent
from apps.organizations.services import create_organization
from apps.projects.models import Project
from tests.test_platform_creation import account, platform_admin
from tests.test_projects import grant


class OwnerGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(username="guard-admin")
        cls.owner = get_user_model().objects.create_superuser(username="guard-owner")
        cls.other = account("guard-other")
        cls.organization = create_organization(user=cls.owner, name="Owner guard workspace")
        cls.second = create_organization(user=cls.owner, name="Second guarded workspace")
        cls.owner_membership = cls.organization.memberships.get(user=cls.owner)
        cls.other_membership = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.other,
        )

    def test_queryset_update_and_save_cannot_deactivate_owner_of_any_organization(self):
        users = get_user_model().objects
        with self.assertRaises(IntegrityError), transaction.atomic():
            users.filter(pk__in=[self.owner.pk, self.other.pk]).update(is_active=False)
        self.owner.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertTrue(self.other.is_active)

        self.owner.is_active = False
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.owner.save(update_fields=["is_active"])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(Organization.objects.filter(owner=self.owner).count(), 2)

    def test_only_nonowners_can_be_deactivated_and_deleted(self):
        users = get_user_model().objects
        with self.assertRaises((ProtectedError, IntegrityError)), transaction.atomic():
            users.filter(pk=self.owner.pk).delete()
        self.assertTrue(users.filter(pk=self.owner.pk, is_active=True).exists())
        users.filter(pk=self.other.pk).update(is_active=False)
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_active)
        self.other.delete()
        self.assertFalse(users.filter(username="guard-other").exists())

    def test_database_cannot_assign_ownership_to_inactive_member(self):
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=self.organization.pk).update(owner=self.other)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.owner_id, self.owner.pk)

    def test_owner_membership_cannot_be_deactivated_or_deleted(self):
        membership = OrganizationMembership.objects.filter(pk=self.owner_membership.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            membership.update(is_active=False)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS organization_owner_active_after_membership_end IMMEDIATE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            membership.delete()
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS organization_owner_active_after_membership_delete IMMEDIATE")
        self.assertTrue(membership.filter(is_active=True).exists())

    def test_django_admin_cannot_deactivate_the_owner(self):
        client = Client()
        client.force_login(self.admin)
        change = reverse("admin:accounts_user_change", args=[self.owner.pk])
        form = client.get(change)
        self.assertEqual(form.status_code, 200)
        response = client.post(change, {
            "username": self.owner.username,
            "email": self.owner.email,
            "is_staff": "on",
            "is_superuser": "on",
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Сначала передайте владение", str(response.context["adminform"].form.errors["is_active"]))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_django_admin_bulk_deactivation_is_atomic_for_mixed_selection(self):
        client = Client()
        client.force_login(self.admin)
        bulk = reverse("admin:accounts_user_changelist")
        response = client.post(bulk, {
            "action": "disable_selected",
            "_selected_action": [self.owner.pk, self.other.pk],
        }, follow=True)
        self.assertContains(response, "Сначала передайте владение")
        self.assertEqual(get_user_model().objects.filter(
            pk__in=[self.owner.pk, self.other.pk], is_active=True,
        ).count(), 2)

    def test_django_admin_single_and_bulk_delete_leave_all_selected_accounts_untouched(self):
        client = Client()
        client.force_login(self.admin)
        single = reverse("admin:accounts_user_delete", args=[self.owner.pk])
        bulk = reverse("admin:accounts_user_changelist")
        self.assertLess(client.post(single, {"post": "yes"}).status_code, 400)
        self.assertLess(client.post(bulk, {
            "action": "delete_selected",
            "_selected_action": [self.owner.pk, self.other.pk],
            "post": "yes",
        }).status_code, 400)
        users = get_user_model().objects
        self.assertTrue(users.filter(pk=self.owner.pk).exists())
        self.assertTrue(users.filter(pk=self.other.pk).exists())


class RecoveryFixture:
    @classmethod
    def make_fixture(cls):
        cls.owner = get_user_model().objects.create_superuser(username="recovery-owner")
        cls.operator = platform_admin("recovery-operator")
        cls.approver = platform_admin("recovery-approver")
        cls.candidate = account("recovery-candidate")
        cls.outsider = account("recovery-outsider")
        cls.organization = create_organization(user=cls.owner, name="Recovery workspace")
        cls.candidate_membership = OrganizationMembership.objects.create(
            organization=cls.organization, user=cls.candidate,
        )
        cls.owner_membership = cls.organization.memberships.get(user=cls.owner)

    def command(self, *, scenario="transfer", expected_owner=None, operator=None, approver=None,
                candidate=None, reason="INC-2026-114", mode="--confirm", **kwargs):
        args = [
            "--organization-id", str(self.organization.pk),
            "--expected-owner", (expected_owner or self.owner).username,
            "--scenario", scenario,
            "--operator", (operator or self.operator).username,
            "--approver", (approver or self.approver).username,
            "--reason", reason,
        ]
        if mode:
            args.append(mode)
        if scenario == "transfer":
            args.extend(["--candidate", (candidate or self.candidate).username])
        stdout = StringIO()
        call_command("recover_organization_owner", *args, stdout=stdout, **kwargs)
        return stdout.getvalue()

    def recovery_events(self):
        return AuditEvent.objects.filter(
            organization=self.organization, action="organization.ownership_recovered",
        )

    def platform_recovery_events(self):
        return PlatformAuditEvent.objects.filter(
            action="platform.ownership_recovered", subject_id=self.organization.pk,
        )


class OwnerRecoveryTests(RecoveryFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.make_fixture()

    def legacy_deactivate_owner(self):
        # The test database predates this guard for the duration of this fixture write.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database = cursor.fetchone()[0]
            self.assertTrue(database.startswith("test_enactus_"))
            cursor.execute(
                "ALTER TABLE accounts_user DISABLE TRIGGER organization_owner_account_deactivation_guard"
            )
            get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
            cursor.execute(
                "ALTER TABLE accounts_user ENABLE TRIGGER organization_owner_account_deactivation_guard"
            )
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)

    def test_dry_run_and_no_confirmation_do_not_write(self):
        output = self.command(mode="--dry-run")
        self.assertIn("Dry run", output)
        self.assertIn(f"{self.owner.pk} -> {self.candidate.pk}", output)
        self.assertNotIn("INC-2026-114", output)
        with self.assertRaises(CommandError):
            self.command(mode=None)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.owner_id, self.owner.pk)
        self.assertFalse(self.recovery_events().exists())
        self.assertFalse(self.platform_recovery_events().exists())

    def test_transfer_preserves_memberships_and_separates_audit_reason(self):
        assignment = grant(self.organization, self.owner_membership, "projects.view")
        project = Project.objects.create(
            organization=self.organization, name="Existing project", created_by=self.owner,
        )
        self.command()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.owner_id, self.candidate.pk)
        self.assertEqual(self.organization.memberships.filter(is_active=True).count(), 2)
        self.assertTrue(self.organization.memberships.filter(pk=self.owner_membership.pk, is_active=True).exists())
        assignment.refresh_from_db()
        self.assertIsNone(assignment.revoked_at)
        project.refresh_from_db()
        self.assertEqual(project.created_by_id, self.owner.pk)
        tenant = self.recovery_events().get()
        self.assertEqual(tenant.changes["owner_id"], {"old": self.owner.pk, "new": self.candidate.pk})
        self.assertNotIn("INC-2026-114", str(tenant.metadata))
        platform = self.platform_recovery_events().get()
        self.assertEqual(platform.actor_id, self.operator.pk)
        self.assertEqual(platform.metadata["approver_id"], self.approver.pk)
        self.assertEqual(platform.metadata["reason"], "INC-2026-114")

    def test_reactivate_legacy_owner_keeps_owner_and_membership(self):
        offer = offer_ownership(
            user=self.owner, org_id=self.organization.pk, recipient_id=self.candidate_membership.pk,
        )
        other_org = create_organization(user=self.owner, name="Second workspace with an old offer")
        other_membership = OrganizationMembership.objects.create(organization=other_org, user=self.candidate)
        other_offer = offer_ownership(
            user=self.owner, org_id=other_org.pk, recipient_id=other_membership.pk,
        )
        self.legacy_deactivate_owner()
        self.command(scenario="reactivate", mode="--dry-run")
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertFalse(self.recovery_events().exists())
        self.command(scenario="reactivate")
        self.owner.refresh_from_db()
        self.organization.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(self.organization.owner_id, self.owner.pk)
        self.assertTrue(self.organization.memberships.filter(pk=self.owner_membership.pk, is_active=True).exists())
        offer.refresh_from_db()
        self.assertEqual(offer.status, OwnershipTransfer.Status.REVOKED)
        other_offer.refresh_from_db()
        self.assertEqual(other_offer.status, OwnershipTransfer.Status.REVOKED)
        self.assertEqual(self.recovery_events().get().changes["is_active"], {"old": False, "new": True})
        self.assertEqual(self.platform_recovery_events().get().metadata["scenario"], "reactivate")
        with self.assertRaises(CommandError):
            self.command(scenario="reactivate")

    def test_rejects_stale_owner_unauthorized_actors_and_ineligible_candidates(self):
        for params in (
            {"expected_owner": self.outsider},
            {"operator": self.owner},
            {"approver": self.owner},
            {"approver": self.operator},
            {"candidate": self.outsider},
            {"candidate": self.owner},
            {"reason": ""},
        ):
            with self.subTest(params=params), self.assertRaises(CommandError):
                self.command(**params)
        get_user_model().objects.filter(pk=self.candidate.pk).update(is_active=False)
        with self.assertRaises(CommandError):
            self.command()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.owner_id, self.owner.pk)
        self.assertFalse(self.recovery_events().exists())
        self.assertFalse(self.platform_recovery_events().exists())

    def test_operator_cannot_make_themselves_owner_even_if_an_active_participant(self):
        OrganizationMembership.objects.create(organization=self.organization, user=self.operator)
        with self.assertRaises(CommandError):
            self.command(candidate=self.operator)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.owner_id, self.owner.pk)
        self.assertFalse(self.platform_recovery_events().exists())

    def test_platform_admin_list_and_explicit_audited_grant_and_revoke(self):
        stdout = StringIO()
        call_command("platform_admin", "list", stdout=stdout)
        self.assertIn(self.operator.username, stdout.getvalue())
        self.assertIn(self.approver.username, stdout.getvalue())
        self.assertNotIn(self.owner.username, stdout.getvalue())
        with self.assertRaises(CommandError):
            call_command("platform_admin", "grant", self.outsider.username, stdout=stdout)
        call_command("platform_admin", "grant", self.outsider.username, "--confirm", stdout=stdout)
        call_command("platform_admin", "revoke", self.outsider.username, "--confirm", stdout=stdout)
        self.assertEqual(list(PlatformAuditEvent.objects.filter(
            subject_id=self.outsider.pk, action__in=["platform.admin_granted", "platform.admin_revoked"],
        ).order_by("pk").values_list("action", flat=True)), ["platform.admin_granted", "platform.admin_revoked"])

    def test_audit_failure_rolls_back_both_recovery_modes(self):
        for scenario in ("transfer", "reactivate"):
            if scenario == "reactivate":
                self.legacy_deactivate_owner()
            with self.subTest(scenario=scenario), patch(
                "apps.organizations.ownership_recovery.record",
                side_effect=RuntimeError("audit failure"),
            ), self.assertRaisesRegex(RuntimeError, "audit failure"):
                self.command(scenario=scenario)
            self.owner.refresh_from_db()
            self.organization.refresh_from_db()
            self.assertEqual(self.owner.is_active, scenario == "transfer")
            self.assertEqual(self.organization.owner_id, self.owner.pk)
            self.assertFalse(self.recovery_events().exists())
            self.assertFalse(self.platform_recovery_events().exists())


class OwnerRecoveryConcurrencyTests(RecoveryFixture, TransactionTestCase):
    def test_competing_recovery_commands_have_one_winner(self):
        self.make_fixture()
        alternate = account("recovery-alternate")
        OrganizationMembership.objects.create(organization=self.organization, user=alternate)
        barrier = Barrier(2)

        def try_transfer(candidate):
            try:
                barrier.wait(timeout=10)
                self.command(candidate=candidate)
                return "applied"
            except CommandError:
                return "denied"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(try_transfer, [self.candidate, alternate]))
        self.assertCountEqual(statuses, ["applied", "denied"])
        self.organization.refresh_from_db()
        self.assertIn(self.organization.owner_id, [self.candidate.pk, alternate.pk])
        self.assertEqual(self.recovery_events().count(), 1)
        self.assertEqual(self.platform_recovery_events().count(), 1)
