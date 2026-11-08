from django.core.management.base import BaseCommand, CommandError

from apps.organizations.ownership_recovery import SCENARIOS, OwnershipRecoveryError, recover_organization_owner


class Command(BaseCommand):
    help = "Recover organization ownership after a documented incident and independent approval."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", "--org-id", dest="organization_id", type=int, required=True)
        parser.add_argument("--expected-owner", required=True, help="Exact username of the current owner.")
        parser.add_argument("--scenario", choices=SCENARIOS, required=True)
        parser.add_argument("--operator", required=True, help="Exact username of the platform operator.")
        parser.add_argument("--approver", required=True, help="Exact username of the independent platform approver.")
        parser.add_argument("--reason", required=True, help="Reference to the offline incident or approval ticket.")
        parser.add_argument("--candidate", "--new-owner", dest="candidate", help="Exact username for transfer.")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true", help="Validate the operation without writing changes.")
        mode.add_argument("--confirm", action="store_true", help="Apply the independently approved change.")

    def handle(self, *args, **options):
        try:
            result = recover_organization_owner(
                organization_id=options["organization_id"], expected_owner=options["expected_owner"],
                scenario=options["scenario"], operator=options["operator"], approver=options["approver"],
                reason=options["reason"], candidate=options["candidate"], dry_run=options["dry_run"],
                confirm=options["confirm"],
            )
        except OwnershipRecoveryError as exc:
            raise CommandError(str(exc)) from None
        summary = (
            f"Organization {result.organization_id}; scenario {result.scenario}; "
            f"owner account {result.old_owner_id} -> {result.new_owner_id}; "
            f"previous owner account was {'active' if result.old_owner_active else 'inactive'}."
        )
        if result.applied:
            self.stdout.write(self.style.SUCCESS(f"Recovery applied. {summary}"))
        else:
            self.stdout.write(f"Dry run passed. {summary}")
