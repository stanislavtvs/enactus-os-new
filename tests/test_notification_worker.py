from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
import signal
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connections, transaction
from django.test import TestCase, TransactionTestCase

from apps.notifications.models import Notification, NotificationEvent
from apps.notifications.services import publish, retry_due, retry_pending
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.tasks.models import Task


NOW = datetime.fromisoformat("2027-11-09T12:00:00+00:00")


def create_target(suffix):
    users = get_user_model().objects
    owner = users.create_superuser(username=f"notification-worker-owner-{suffix}")
    user = users.create_user(username=f"notification-worker-target-{suffix}")
    org = create_organization(user=owner, name=f"Notification worker {suffix}")
    member = OrganizationMembership.objects.create(organization=org, user=user)
    task = Task.objects.create(
        organization=org, created_by=org.memberships.get(user=owner), assignee=member,
        name="Worker delivery target",
    )
    return owner, user, org, task


def pending_event(owner, user, org, task, key, **fields):
    return NotificationEvent.objects.create(
        organization=org, actor=owner, kind="task.assigned", category="tasks",
        object_type="task", object_id=task.pk, idempotency_key=key, recipient_ids=[user.pk],
        **fields,
    )


class NotificationWorkerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner, cls.user, cls.org, cls.task = create_target("ordinary")

    def pending(self, key, **fields):
        return pending_event(self.owner, self.user, self.org, self.task, key, **fields)

    def test_exponential_backoff_terminal_failure_and_manual_recovery(self):
        event = self.pending("backoff")
        moments = [NOW]
        with patch("apps.notifications.services._deliver_notifications", side_effect=IntegrityError("temporary")):
            for attempt, delay in enumerate((60, 120, 240, 480, None), 1):
                with self.subTest(attempt=attempt):
                    current = moments[-1]
                    result = retry_due(now=current)
                    self.assertEqual(result["processed"], 1)
                    event.refresh_from_db()
                    self.assertEqual(event.delivery_attempts, attempt)
                    self.assertEqual(event.last_attempt_at, current)
                    self.assertIsNone(event.delivered_at)
                    self.assertTrue(event.last_error)
                    if delay is None:
                        self.assertEqual(event.failed_at, current)
                        self.assertIsNone(event.next_attempt_at)
                    else:
                        self.assertIsNone(event.failed_at)
                        next_at = current + timedelta(seconds=delay)
                        self.assertEqual(event.next_attempt_at, next_at)
                        self.assertEqual(retry_due(now=next_at - timedelta(seconds=1))["processed"], 0)
                        moments.append(next_at)
                    repeated = publish(
                        organization_id=self.org.pk, actor_id=self.owner.pk,
                        kind="task.assigned", object_type="task", object_id=self.task.pk,
                        recipient_ids=[self.user.pk], key=event.idempotency_key, now=current,
                    )
                    self.assertEqual(repeated.delivery_attempts, attempt)
        self.assertEqual(retry_due(now=moments[-1] + timedelta(days=1))["processed"], 0)
        self.assertFalse(event.notifications.exists())

        retry_pending(limit=10, now=moments[-1] + timedelta(seconds=1))
        event.refresh_from_db()
        self.assertEqual(event.delivery_attempts, 6)
        self.assertIsNotNone(event.delivered_at)
        self.assertIsNone(event.failed_at)
        self.assertIsNone(event.next_attempt_at)
        self.assertEqual(event.last_error, "")
        self.assertEqual(list(event.notifications.values_list("recipient_id", flat=True)), [self.user.pk])
        self.assertEqual(retry_due(now=moments[-1] + timedelta(days=2))["processed"], 0)

    def test_restart_recovers_persisted_retry_without_duplicate_delivery(self):
        event = self.pending("restart")

        def partial_failure(current):
            Notification.objects.create(organization=self.org, event=current, recipient=self.user)
            raise IntegrityError("temporary after partial insert")

        with patch("apps.notifications.services._deliver_notifications", side_effect=partial_failure):
            failed = retry_due(limit=1, now=NOW)
        self.assertEqual(failed["processed"], 1)
        self.assertEqual(failed["delivered"], 0)
        self.assertFalse(event.notifications.exists())
        event.refresh_from_db()
        scheduled = event.next_attempt_at
        self.assertEqual(scheduled, NOW + timedelta(minutes=1))
        self.assertEqual(retry_due(limit=1, now=NOW)["processed"], 0)
        self.assertEqual(retry_due(limit=1, now=scheduled)["delivered"], 1)
        self.assertEqual(retry_due(limit=1, now=scheduled)["processed"], 0)
        event.refresh_from_db()
        self.assertEqual(event.delivered_at, scheduled)
        self.assertEqual(event.delivery_attempts, 2)
        self.assertEqual(event.notifications.count(), 1)

    def test_retry_does_not_deliver_to_member_who_lost_access(self):
        event = self.pending("revoked")
        OrganizationMembership.objects.filter(organization=self.org, user=self.user).update(is_active=False)
        result = retry_due(limit=10, now=NOW)
        self.assertEqual(result["processed"], 1)
        event.refresh_from_db()
        self.assertIsNotNone(event.delivered_at)
        self.assertFalse(event.notifications.exists())

    def test_deferred_event_is_not_processed_early_or_bypassed_by_publish_replay(self):
        fields = {
            "organization_id": self.org.pk, "actor_id": self.owner.pk,
            "kind": "task.assigned", "object_type": "task", "object_id": self.task.pk,
            "recipient_ids": [self.user.pk], "key": "deferred",
        }
        future = NOW + timedelta(hours=1)
        event = publish(**fields, now=NOW, deliver_after=future)
        self.assertEqual(event.next_attempt_at, future)
        self.assertEqual(event.delivery_attempts, 0)
        self.assertIsNone(publish(**fields, now=NOW).delivered_at)
        self.assertEqual(retry_due(now=future - timedelta(seconds=1))["processed"], 0)
        self.assertEqual(retry_due(now=future)["delivered"], 1)
        self.assertEqual(event.notifications.count(), 1)

    def test_worker_once_and_sigterm_process_bounded_batches_and_restore_handlers(self):
        module = "apps.notifications.management.commands.notification_worker"
        with (
            patch(f"{module}.retry_due") as retry,
            patch(f"{module}.run_reminders") as reminders,
            patch(f"{module}.close_old_connections"),
        ):
            output = StringIO()
            call_command("notification_worker", once=True, limit=3, stdout=output)
            retry.assert_called_once_with(limit=3)
            reminders.assert_called_once_with(limit=3)
            self.assertIn("Notification batch complete", output.getvalue())

            retry.reset_mock()
            reminders.reset_mock()
            handlers = {}
            old = {signal.SIGINT: signal.SIG_IGN, signal.SIGTERM: signal.SIG_DFL}

            def install(signum, handler):
                handlers[signum] = handler
                return old[signum]

            def stop_during_retry(**kwargs):
                handlers[signal.SIGTERM](signal.SIGTERM, None)

            retry.side_effect = stop_during_retry
            with patch(f"{module}.signal.signal", side_effect=install):
                call_command("notification_worker", limit=3)
            retry.assert_called_once_with(limit=3)
            reminders.assert_not_called()
            self.assertEqual(handlers, old)


class ConcurrentNotificationWorkerTests(TransactionTestCase):
    def run_worker(self, start=None):
        worker_connection = connections["default"]
        try:
            with worker_connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            if start is not None:
                start.wait()
            return retry_due(limit=10, now=NOW)
        finally:
            worker_connection.close()

    def test_worker_skips_locked_row_and_delivers_another_event(self):
        owner, user, org, task = create_target("locked")
        first = pending_event(owner, user, org, task, "locked-first")
        second = pending_event(owner, user, org, task, "available-second")
        with transaction.atomic():
            NotificationEvent.objects.select_for_update().get(pk=first.pk)
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.run_worker).result(timeout=8)
            self.assertEqual(result["delivered"], 1)
            self.assertFalse(first.notifications.exists())
            self.assertEqual(second.notifications.count(), 1)
        self.assertEqual(retry_due(limit=10, now=NOW)["delivered"], 1)
        self.assertEqual(first.notifications.count(), 1)

    def test_competing_workers_make_one_delivery_attempt_for_same_event(self):
        owner, user, org, task = create_target("concurrent")
        event = pending_event(owner, user, org, task, "competing-workers")
        start = Barrier(2, timeout=8)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_worker, start) for _ in range(2)]
            results = [future.result(timeout=16) for future in futures]
        self.assertEqual(sum(result["processed"] for result in results), 1)
        event.refresh_from_db()
        self.assertEqual(event.delivery_attempts, 1)
        self.assertEqual(event.notifications.count(), 1)
