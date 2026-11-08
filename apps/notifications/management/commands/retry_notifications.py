from django.core.management.base import BaseCommand, CommandError

from apps.notifications.services import retry_pending


class Command(BaseCommand):
    help = "Retry pending internal notification events."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        try:
            result = retry_pending(limit=options["limit"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Processed {result['processed']}, delivered {result['delivered']}, still pending {result['pending']}."
        )
