import signal
from time import monotonic, sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections

from apps.accounts.reset_queue import process_next_password_reset
from apps.notifications.services import retry_due


def run_reminders(*, limit):
    from apps.notifications.reminders import run_reminders as scan

    return scan(limit=limit)


class Command(BaseCommand):
    help = "Retry due notification deliveries and scan for reminders."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
        parser.add_argument("--interval", type=int, default=15, help="Seconds between batches (1–60).")
        parser.add_argument("--limit", type=int, default=100, help="Batch limit for each operation (1–1000).")

    def handle(self, *args, **options):
        interval = options["interval"]
        limit = options["limit"]
        once = options["once"]
        if not 1 <= interval <= 60:
            raise CommandError("--interval must be between 1 and 60 seconds.")
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")

        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        previous_handlers = {}
        if not once:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, stop)

        try:
            while not stopping:
                close_old_connections()
                try:
                    retry_due(limit=limit)
                    if not stopping:
                        run_reminders(limit=limit)
                    if not stopping:
                        process_next_password_reset()
                except OperationalError as exc:
                    if once:
                        raise CommandError("Notification worker could not complete the database operations.") from exc
                    self.stderr.write("Notification worker could not complete the database operations. Retrying.")
                finally:
                    close_old_connections()

                if once:
                    self.stdout.write("Notification batch complete.")
                    return

                next_batch_at = monotonic() + interval
                # Keep the idle wait interruptible without doing any database work in the signal handler.
                while not stopping and (remaining := next_batch_at - monotonic()) > 0:
                    sleep(min(1, remaining))
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
