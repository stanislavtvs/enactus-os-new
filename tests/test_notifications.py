from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.notifications.models import Notification, NotificationEvent
from apps.notifications.services import publish, retry_pending
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.tasks.models import Task
from tests.test_collection_pages import CollectionMarkup
from tests.test_department_memberships import write


class NotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="notification-owner")
        cls.other_owner = users.create_superuser(username="notification-other-owner")
        cls.recipient = users.create_user(username="notification-recipient")
        cls.outsider = users.create_user(username="notification-outsider")
        cls.org = create_organization(user=cls.owner, name="Notification workspace")
        cls.other_org = create_organization(user=cls.other_owner, name="Other notification workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.other_owner_member = cls.other_org.memberships.get(user=cls.other_owner)
        cls.recipient_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.recipient)
        cls.other_recipient_member = OrganizationMembership.objects.create(
            organization=cls.other_org, user=cls.recipient,
        )
        cls.outsider_member = OrganizationMembership.objects.create(organization=cls.org, user=cls.outsider)
        cls.task = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, assignee=cls.recipient_member,
            name="Private notification task", description="Confidential task description",
        )
        cls.other_task = Task.objects.create(
            organization=cls.other_org, created_by=cls.other_owner_member,
            assignee=cls.other_recipient_member, name="Second notification task",
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = "/api/v1/notifications/"

    def publish_task(self, key, *, other=False, task=None, recipients=None, actor=None, data=None):
        task = task or (self.other_task if other else self.task)
        actor = actor or (self.other_owner if other else self.owner)
        publish(
            organization_id=task.organization_id, actor_id=actor.pk, kind="task.assigned",
            object_type="task", object_id=task.pk, key=key,
            recipient_ids=recipients if recipients is not None else [self.recipient.pk], data=data,
        )
        return NotificationEvent.objects.get(organization_id=task.organization_id, idempotency_key=key)

    def list(self, **params):
        result = self.client.get(self.url, params)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def test_delivery_deduplicates_retries_and_recipients_and_excludes_actor(self):
        event = self.publish_task(
            "task:replayed",
            recipients=[self.recipient.pk, self.recipient.pk, self.owner.pk, self.other_owner.pk],
            data={"title": "Private copied name", "stage_id": 1, "review_id": True, "occurrence_key": "bad"},
        )
        self.publish_task("task:replayed")
        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(list(event.notifications.values_list("recipient_id", flat=True)), [self.recipient.pk])
        self.assertEqual(event.data, {"stage_id": 1})
        self.assertIsNotNone(event.delivered_at)
        self.assertEqual(event.delivery_attempts, 1)
        self.publish_task("task:replayed", other=True)
        self.assertEqual(NotificationEvent.objects.count(), 2)
        self.assertEqual(Notification.objects.count(), 2)

    def test_delivery_rolls_back_with_business_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "business rollback"):
            with transaction.atomic():
                Task.objects.filter(pk=self.task.pk).update(name="Rolled back task title")
                self.publish_task("task:rolled-back")
                self.assertTrue(Notification.objects.filter(event__idempotency_key="task:rolled-back").exists())
                raise RuntimeError("business rollback")
        self.assertFalse(NotificationEvent.objects.filter(idempotency_key="task:rolled-back").exists())
        self.assertFalse(Notification.objects.filter(event__idempotency_key="task:rolled-back").exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.name, "Private notification task")

    def test_failed_delivery_retains_event_for_retry_without_rolling_back_business_action(self):
        with patch(
            "apps.notifications.services._deliver_notifications",
            side_effect=IntegrityError("fake delivery failure"),
        ):
            with transaction.atomic():
                Task.objects.filter(pk=self.task.pk).update(name="Committed despite delivery error")
                event = self.publish_task("task:delivery-retry")
        self.task.refresh_from_db()
        self.assertEqual(self.task.name, "Committed despite delivery error")
        self.assertIsNone(event.delivered_at)
        self.assertEqual(event.delivery_attempts, 1)
        self.assertTrue(event.last_error)
        self.assertFalse(event.notifications.exists())
        retry_pending(limit=20)
        retry_pending(limit=20)
        event.refresh_from_db()
        self.assertIsNotNone(event.delivered_at)
        self.assertEqual(event.delivery_attempts, 2)
        self.assertEqual(event.notifications.count(), 1)

    def test_api_filters_pages_and_counts_across_organizations_without_switching_workspace(self):
        for index in range(3):
            self.publish_task(f"task:pagination-{index}")
        self.publish_task("task:other-org", other=True)
        self.client.force_login(self.recipient)
        session = self.client.session
        session["active_workspace_id"] = self.org.pk
        session.save()

        page_one = self.list(category="tasks", page=1, page_size=2)
        page_two = self.list(category="tasks", page=2, page_size=2)
        self.assertEqual(page_one["total"], 4)
        self.assertEqual(page_one["page"], 1)
        self.assertEqual(page_one["page_size"], 2)
        self.assertEqual(len(page_two["items"]), 2)
        self.assertFalse({row["id"] for row in page_one["items"]} & {row["id"] for row in page_two["items"]})
        self.assertCountEqual([row["id"] for row in page_one["organizations"]], [self.org.pk, self.other_org.pk])
        self.assertEqual(self.list(org_id=self.other_org.pk)["total"], 1)
        self.assertEqual(self.list(category="calendar")["items"], [])
        self.assertEqual(self.client.get(f"{self.url}count/").json()["unread"], 4)
        self.assertEqual(self.client.session["active_workspace_id"], self.org.pk)

    def test_read_and_mark_all_are_recipient_scoped_csrf_protected_and_idempotent(self):
        first = self.publish_task("task:read-first").notifications.get()
        second = self.publish_task("task:read-second").notifications.get()
        other = self.publish_task("task:read-other", other=True).notifications.get()
        read_url = f"{self.url}{first.pk}/read/"
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(write(self.client, "post", read_url).status_code, 401)
        self.client.force_login(self.outsider)
        self.assertEqual(self.list()["items"], [])
        self.assertEqual(write(self.client, "post", read_url).status_code, 404)
        self.assertEqual(self.client.get(f"{self.url}{first.pk}/link/").status_code, 404)

        self.client.force_login(self.recipient)
        self.assertEqual(write(self.client, "post", read_url, csrf=False).status_code, 403)
        read = write(self.client, "post", read_url)
        self.assertEqual(read.status_code, 200, read.content)
        self.assertEqual(read.json()["unread"], 2)
        self.assertIsNotNone(read.json()["read_at"])
        again = write(self.client, "post", read_url)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(read.json()["read_at"], again.json()["read_at"])
        self.assertEqual(self.list(unread=True)["total"], 2)

        all_url = f"{self.url}read-all/?org_id={self.org.pk}&category=tasks"
        self.assertEqual(write(self.client, "post", all_url, csrf=False).status_code, 403)
        marked = write(self.client, "post", all_url)
        self.assertEqual(marked.status_code, 200, marked.content)
        self.assertEqual(marked.json(), {"updated": 1, "unread": 1})
        self.assertEqual(write(self.client, "post", all_url).json(), {"updated": 0, "unread": 1})
        second.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(second.read_at)
        self.assertIsNone(other.read_at)
        self.assertEqual(self.list()["total"], 3)

    def test_link_and_listing_recheck_access_without_leaking_old_object_data(self):
        notification = self.publish_task("task:link-access").notifications.get()
        self.client.force_login(self.recipient)
        initial = self.list()["items"][0]
        self.assertTrue(initial["available"])
        self.assertIn(self.task.name, f"{initial['title']} {initial['description']}")
        link = self.client.get(f"{self.url}{notification.pk}/link/")
        self.assertEqual(link.status_code, 200, link.content)
        self.assertTrue(link.json()["url"].endswith(f"/tasks/{self.task.pk}/"))

        OrganizationMembership.objects.filter(pk=self.recipient_member.pk).update(is_active=False)
        lost = self.list()["items"][0]
        self.assertFalse(lost["available"])
        self.assertIsNone(lost["organization_id"])
        self.assertIsNone(lost["organization_name"])
        self.assertNotIn(self.task.name, str(lost))
        self.assertNotIn(self.task.description, str(lost))
        self.assertEqual(self.client.get(f"{self.url}{notification.pk}/link/").status_code, 404)

    def test_database_rejects_delivery_tenant_mismatch_and_duplicate_event_recipient(self):
        event = self.publish_task("task:db-guards")
        for organization in (self.org, self.other_org):
            recipient = self.recipient if organization == self.org else self.outsider
            with self.subTest(organization=organization.pk), self.assertRaises(IntegrityError), transaction.atomic():
                Notification.objects.create(organization=organization, event=event, recipient=recipient)
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(event.notifications.count(), 1)

    def test_listing_queries_do_not_grow_per_notification(self):
        self.publish_task("task:query-baseline")
        self.client.force_login(self.recipient)
        with CaptureQueriesContext(connection) as baseline:
            self.assertEqual(self.list()["total"], 1)
        for index in range(8):
            task = Task.objects.create(
                organization=self.org, created_by=self.owner_member,
                assignee=self.recipient_member, name=f"Distinct task {index}",
            )
            self.publish_task(f"task:query-{index}", task=task)
        with CaptureQueriesContext(connection) as expanded:
            self.assertEqual(self.list()["total"], 9)
        self.assertLessEqual(len(expanded), len(baseline) + 2)

    def test_notification_page_and_navigation_preserve_workspace_and_accessibility(self):
        url = reverse("notifications")
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")
        self.client.force_login(self.recipient)
        session = self.client.session
        session["active_workspace_id"] = self.org.pk
        session.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        markup = CollectionMarkup(response)
        self.assertEqual(markup.ids["notification-nav"][1]["href"], url)
        self.assertIn("hidden", markup.ids["notification-nav-badge"][1])
        self.assertEqual(markup.ids["notification-center"][1]["data-api-url"], self.url)
        self.assertEqual(markup.ids["notification-status"][1]["role"], "status")
        self.assertEqual(markup.ids["notification-error"][1]["role"], "alert")
        self.assertTrue({"notification-category", "notification-organization"} <= markup.labels)
        self.assertIn("disabled", markup.ids["notification-read-all"][1])
        self.assertEqual(self.client.session["active_workspace_id"], self.org.pk)


class ConcurrentNotificationTests(TransactionTestCase):
    def test_simultaneous_publishes_produce_one_event_and_one_notification(self):
        users = get_user_model().objects
        owner = users.create_superuser(username="notification-race-owner")
        user = users.create_user(username="notification-race-recipient")
        org = create_organization(user=owner, name="Notification race")
        member = OrganizationMembership.objects.create(organization=org, user=user)
        task = Task.objects.create(
            organization=org, created_by=org.memberships.get(user=owner), assignee=member,
            name="Race task",
        )
        start = Barrier(2, timeout=8)

        def send():
            worker_connection = connections["default"]
            try:
                with worker_connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                start.wait()
                publish(
                    organization_id=org.pk, actor_id=owner.pk, kind="task.assigned",
                    object_type="task", object_id=task.pk,
                    recipient_ids=[user.pk], key="task:delivery-race",
                )
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send) for _ in range(2)]
            for future in futures:
                future.result(timeout=16)
        event = NotificationEvent.objects.get(organization=org)
        self.assertIsNotNone(event.delivered_at)
        self.assertEqual(event.delivery_attempts, 1)
        self.assertEqual(list(event.notifications.values_list("recipient_id", flat=True)), [user.pk])
