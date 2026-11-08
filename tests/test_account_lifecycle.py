import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from urllib.parse import urlsplit
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.access.models import RoleAssignment
from apps.accounts.models import AuthThrottle, EmailVerification
from apps.accounts.services import account_reset_token_generator, confirm_verification, token_digest
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.services import create_organization


PASSWORD = "CorrectHorse!Paper7"
NEW_PASSWORD = "AnotherStrong!River9"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountLifecycleTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.users = get_user_model().objects

    def form(self, name, data, *, client=None, csrf=True, path=None, **headers):
        client = client or self.client
        path = path or reverse(name)
        if csrf:
            client.get(path)
            headers["X-CSRFToken"] = client.cookies[settings.CSRF_COOKIE_NAME].value
        peer = headers.pop("REMOTE_ADDR", None)
        extra = {"REMOTE_ADDR": peer} if peer is not None else {}
        return client.post(path, data, headers=headers, **extra)

    def register(self, username="new-account", email="new-account@example.invalid", **fields):
        return self.form("register", {
            "username": username, "email": email, "password1": PASSWORD, "password2": PASSWORD, **fields,
        })

    def login(self, username, password=PASSWORD, *, client=None, **fields):
        return self.form("login", {"username": username, "password": password, **fields}, client=client)

    def mail_path(self, message=None):
        message = message or mail.outbox[-1]
        links = re.findall(r'https?://[^\s<>"]+', message.body)
        self.assertEqual(len(links), 1)
        return urlsplit(links[0]).path

    def follow_confirmation(self, link, *, client=None, password=PASSWORD):
        client = client or self.client
        response = client.get(link)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(urlsplit(response["Location"]).path, reverse("verify_email"))
        self.assertNotIn(link.rsplit("/", 2)[-2], response["Location"])
        return self.form("verify_email", {"password": password}, client=client)

    def register_and_verify(self, username="new-account", email="new-account@example.invalid"):
        response = self.register(username, email)
        self.assertEqual(response.status_code, 302, response.content)
        path = self.mail_path()
        self.follow_confirmation(path)
        return self.users.get(username=username)

    def test_registration_requires_csrf_and_rejects_bad_username_email_and_password(self):
        self.assertEqual(self.form("register", {
            "username": "csrf-user", "email": "csrf@example.invalid",
            "password1": PASSWORD, "password2": PASSWORD,
        }, csrf=False).status_code, 403)
        for fields in (
            {"username": "invalid user"},
            {"email": "invalid email"},
            {"password2": NEW_PASSWORD},
            {"password1": "123", "password2": "123"},
        ):
            with self.subTest(fields=fields):
                payload = {
                    "username": "validation-user", "email": "validation@example.invalid",
                    "password1": PASSWORD, "password2": PASSWORD,
                }
                response = self.form("register", payload | fields)
                self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.users.count(), 0)
        self.assertEqual(mail.outbox, [])

    @override_settings(ACCOUNT_PUBLIC_ORIGIN="https://accounts.example.invalid")
    def test_registration_confirmation_and_login_do_not_create_roles_memberships_or_organization(self):
        response = self.register(is_staff=True, is_superuser=True, is_active=True)
        self.assertEqual(response.status_code, 302, response.content)
        user = self.users.get(username="new-account")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_active)
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new-account@example.invalid"])
        self.assertIn("https://accounts.example.invalid/accounts/verify-email/", mail.outbox[0].body)
        self.assertEqual(self.login(user.username).status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

        path = self.mail_path()
        token_page = self.client.get(path)
        self.assertEqual(token_page.status_code, 302, token_page.content)
        self.assertEqual(token_page.headers["Referrer-Policy"], "no-referrer")
        clean_page = self.client.get(token_page["Location"])
        self.assertEqual(clean_page.status_code, 200)
        self.assertEqual(clean_page.headers["Referrer-Policy"], "same-origin")
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(self.form("verify_email", {}, csrf=False).status_code, 403)
        wrong = self.form("verify_email", {"password": NEW_PASSWORD})
        self.assertEqual(wrong.status_code, 200, wrong.content)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        confirmed = self.form("verify_email", {"password": PASSWORD})
        self.assertLess(confirmed.status_code, 400, confirmed.content)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(user.registration_pending)

        logged_in = self.login(user.username, next="https://untrusted.invalid/")
        self.assertEqual(logged_in.status_code, 302, logged_in.content)
        self.assertNotEqual(urlsplit(logged_in["Location"]).hostname, "untrusted.invalid")
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertFalse(Organization.objects.exists())
        self.assertFalse(OrganizationMembership.objects.filter(user=user).exists())
        self.assertFalse(RoleAssignment.objects.filter(organization_membership__user=user).exists())
        self.assertEqual(self.client.get("/api/v1/organizations/").json(), [])

    def test_legacy_account_with_unverified_or_empty_email_can_still_log_in_without_membership_loss(self):
        owner = self.users.create_superuser(username="legacy-owner", email="", password=PASSWORD)
        existing = self.users.create_user(username="legacy-unverified", email="Legacy@Example.invalid", password=PASSWORD)
        duplicate = self.users.create_user(username="legacy-duplicate", email="legacy@example.invalid", password=PASSWORD)
        organization = create_organization(user=owner, name="Existing organization")
        for user in (owner, existing, duplicate):
            with self.subTest(user=user.username):
                client = Client(enforce_csrf_checks=True)
                response = self.login(user.username, client=client)
                self.assertEqual(response.status_code, 302, response.content)
                self.assertEqual(client.session["_auth_user_id"], str(user.pk))
        self.assertTrue(organization.memberships.get(user=owner).is_active)
        self.assertEqual(Organization.objects.get(pk=organization.pk).owner_id, owner.pk)

    def test_resend_rotates_digest_and_used_or_expired_links_do_not_activate(self):
        created = self.register()
        self.assertEqual(created.status_code, 302, created.content)
        user = self.users.get(username="new-account")
        expired_path = self.mail_path()
        original = EmailVerification.objects.get(user=user)
        token = expired_path.rstrip("/").rsplit("/", 1)[-1]
        self.assertNotEqual(original.token_digest, token)
        self.assertRegex(original.token_digest, r"^[0-9a-f]{64}$")
        EmailVerification.objects.filter(user=user).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.follow_confirmation(expired_path)
        user.refresh_from_db()
        self.assertTrue(user.registration_pending)

        self.form("resend_verification", {"username": user.username, "email": user.email})
        self.assertEqual(len(mail.outbox), 2)
        fresh_path = self.mail_path()
        self.assertNotEqual(expired_path, fresh_path)
        self.assertNotEqual(original.token_digest, EmailVerification.objects.get(user=user).token_digest)
        self.follow_confirmation(expired_path)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.follow_confirmation(fresh_path)
        user.refresh_from_db()
        verified_at = user.email_verified_at
        self.assertIsNotNone(verified_at)
        self.assertFalse(EmailVerification.objects.filter(user=user).exists())
        self.follow_confirmation(fresh_path)
        user.refresh_from_db()
        self.assertEqual(user.email_verified_at, verified_at)
        self.assertEqual(self.users.filter(email__iexact=user.email).count(), 1)

    def test_other_authenticated_account_cannot_confirm_pending_registration(self):
        registered = self.register()
        self.assertEqual(registered.status_code, 302, registered.content)
        pending = self.users.get(username="new-account")
        other = self.users.create_user(username="other-account", email="other@example.invalid", password=PASSWORD)
        path = self.mail_path()
        self.client.force_login(other)
        self.follow_confirmation(path)
        pending.refresh_from_db()
        self.assertTrue(pending.registration_pending)
        self.assertIsNone(pending.email_verified_at)
        self.assertEqual(self.client.session["_auth_user_id"], str(other.pk))
        anonymous = Client(enforce_csrf_checks=True)
        self.follow_confirmation(path, client=anonymous)
        pending.refresh_from_db()
        self.assertIsNotNone(pending.email_verified_at)

    def test_verified_address_is_unique_case_insensitively_but_legacy_duplicates_remain(self):
        email = "verified@example.invalid"
        verified = self.users.create_user(
            username="verified", email=email, password=PASSWORD, email_verified_at=timezone.now(),
        )
        legacy = self.users.create_user(username="duplicate-legacy", email=email.upper(), password=PASSWORD)
        self.assertIsNone(legacy.email_verified_at)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.users.filter(pk=legacy.pk).update(email_verified_at=timezone.now())
        response = self.register("conflicting", email.upper())
        self.assertLess(response.status_code, 400, response.content)
        self.assertFalse(EmailVerification.objects.filter(target_email__iexact=email).exists())
        self.assertEqual(mail.outbox, [])
        verified.refresh_from_db()
        self.assertEqual(verified.email, email)
        self.assertTrue(verified.check_password(PASSWORD))

    def test_reset_request_is_neutral_for_unknown_unverified_and_disabled_accounts(self):
        verified = self.users.create_user(
            username="reset-verified", email="reset@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(),
        )
        self.users.create_user(username="reset-legacy", email="legacy-reset@example.invalid", password=PASSWORD)
        self.users.create_user(
            username="reset-disabled", email="disabled-reset@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(), is_active=False,
        )
        observed = []
        for address in (verified.email.upper(), "legacy-reset@example.invalid", "disabled-reset@example.invalid",
                        "unknown-reset@example.invalid"):
            with self.subTest(address=address):
                response = self.form("password_reset", {"email": address})
                self.assertEqual(response.status_code, 302, response.content)
                observed.append(response["Location"])
        self.assertEqual(observed, [reverse("password_reset_done")] * 4)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [verified.email])
        self.assertNotIn(PASSWORD, mail.outbox[0].body)

    def test_django_reset_invalidates_old_password_sessions_and_replay_without_touching_roles(self):
        user = self.users.create_superuser(
            username="reset-owner", email="reset-owner@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(),
        )
        organization = create_organization(user=user, name="Reset keeps tenant ownership")
        existing_session = Client(enforce_csrf_checks=True)
        existing_session.force_login(user)
        self.assertEqual(existing_session.get(reverse("user-profile")).status_code, 200)
        self.assertEqual(self.form("password_reset", {"email": user.email}).status_code, 302)
        token_path = self.mail_path()
        landing = self.client.get(token_path)
        self.assertEqual(landing.status_code, 302, landing.content)
        clean = urlsplit(landing["Location"]).path
        self.assertIn("set-password", clean)
        self.assertNotIn(token_path.rstrip("/").rsplit("/", 1)[-1], clean)
        reset = self.form("password_reset_confirm", {
            "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        }, path=clean)
        self.assertEqual(reset.status_code, 302, reset.content)
        self.assertEqual(urlsplit(reset["Location"]).path, reverse("password_reset_complete"))
        user.refresh_from_db()
        self.assertFalse(user.check_password(PASSWORD))
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(existing_session.get(reverse("user-profile")).status_code, 302)
        self.assertNotIn("_auth_user_id", existing_session.session)
        replay = self.client.get(token_path)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(self.login(user.username).status_code, 200)
        self.assertEqual(self.login(user.username, NEW_PASSWORD).status_code, 302)
        self.assertTrue(organization.memberships.get(user=user).is_active)
        self.assertEqual(Organization.objects.get(pk=organization.pk).owner_id, user.pk)

    def test_expired_reset_token_never_changes_password(self):
        user = self.users.create_user(
            username="reset-expiry", email="reset-expiry@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(),
        )
        self.assertEqual(self.form("password_reset", {"email": user.email}).status_code, 302)
        path = self.mail_path()
        future = account_reset_token_generator._now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 1)
        with patch.object(account_reset_token_generator, "_now", return_value=future):
            expired = self.client.get(path)
            self.assertEqual(expired.status_code, 200, expired.content)
            result = self.form("password_reset_confirm", {
                "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
            }, path=path)
            self.assertEqual(result.status_code, 200, result.content)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(path).status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_password_change_preserves_only_current_session_and_invalidates_reset_link(self):
        user = self.users.create_user(
            username="password-change", email="password-change@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(),
        )
        self.assertEqual(self.form("password_reset", {"email": user.email}).status_code, 302)
        reset_path = self.mail_path()
        another = Client(enforce_csrf_checks=True)
        another.force_login(user)
        self.client.force_login(user)
        invalid = self.form("password_change", {
            "old_password": NEW_PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        })
        self.assertEqual(invalid.status_code, 200, invalid.content)
        changed = self.form("password_change", {
            "old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        })
        self.assertEqual(changed.status_code, 302, changed.content)
        self.assertEqual(urlsplit(changed["Location"]).path, reverse("password_change_done"))
        self.assertEqual(self.client.get(reverse("user-profile")).status_code, 200)
        self.assertEqual(another.get(reverse("user-profile")).status_code, 302)
        self.assertEqual(another.get(reset_path).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(user.check_password(PASSWORD))

    def test_email_change_keeps_verified_address_until_confirmation_and_invalidates_old_reset(self):
        old = "old-mail@example.invalid"
        new = "new-mail@example.invalid"
        user = self.users.create_user(
            username="change-address", email=old, password=PASSWORD, email_verified_at=timezone.now(),
        )
        self.assertEqual(self.form("password_reset", {"email": old}).status_code, 302)
        old_reset = self.mail_path()
        self.client.force_login(user)
        csrf = self.form("change_email", {"email": new, "password": PASSWORD}, csrf=False)
        self.assertEqual(csrf.status_code, 403)
        wrong = self.form("change_email", {"email": new, "password": NEW_PASSWORD})
        self.assertEqual(wrong.status_code, 200, wrong.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(EmailVerification.objects.filter(user=user).exists())

        requested = self.form("change_email", {"email": new.upper(), "password": PASSWORD})
        self.assertEqual(requested.status_code, 302, requested.content)
        self.assertEqual(mail.outbox[-1].to, [new])
        change_path = self.mail_path()
        user.refresh_from_db()
        self.assertEqual(user.email, old)
        self.assertIsNotNone(user.email_verified_at)
        anonymous = Client(enforce_csrf_checks=True)
        self.assertEqual(self.form("password_reset", {"email": new}, client=anonymous).status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        self.follow_confirmation(change_path)
        user.refresh_from_db()
        self.assertEqual(user.email, new)
        self.assertIsNotNone(user.email_verified_at)
        self.assertEqual(anonymous.get(old_reset).status_code, 200)
        self.assertEqual(self.form("password_reset", {"email": new}, client=anonymous).status_code, 302)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(mail.outbox[-1].to, [new])
        requested_back = self.form("change_email", {"email": old, "password": PASSWORD})
        self.assertEqual(requested_back.status_code, 302, requested_back.content)
        self.follow_confirmation(self.mail_path())
        user.refresh_from_db()
        self.assertEqual(user.email, old)
        self.assertEqual(anonymous.get(old_reset).status_code, 200)

    def test_mail_transport_failure_preserves_verified_address_and_neutral_reset(self):
        user = self.users.create_user(
            username="mail-error", email="trusted@example.invalid", password=PASSWORD,
            email_verified_at=timezone.now(),
        )
        self.client.force_login(user)
        with patch("apps.accounts.mail.send_mail", side_effect=OSError("private fake SMTP credential")):
            with self.assertLogs("apps.accounts.mail", level="WARNING") as logs:
                response = self.form("change_email", {"email": "pending@example.invalid", "password": PASSWORD})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertNotContains(response, "private fake SMTP credential")
                anonymous = Client(enforce_csrf_checks=True)
                reset = self.form("password_reset", {"email": user.email}, client=anonymous)
                self.assertEqual(reset.status_code, 302, reset.content)
                self.assertEqual(reset["Location"], reverse("password_reset_done"))
        self.assertNotIn("private fake SMTP credential", str(logs.output))
        user.refresh_from_db()
        self.assertEqual(user.email, "trusted@example.invalid")
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(EmailVerification.objects.filter(user=user).exists())
        self.assertEqual(mail.outbox, [])

    @override_settings(ACCOUNT_TRUSTED_PROXY_NETWORKS=())
    def test_throttling_is_shared_across_clients_and_ignores_spoofed_forwarded_ip(self):
        for attempt in range(11):
            client = Client(enforce_csrf_checks=True)
            result = self.form("login", {"username": "no-account", "password": PASSWORD}, client=client,
                               REMOTE_ADDR="198.51.100.10", **{"X-Real-IP": f"203.0.113.{attempt + 1}"})
            self.assertEqual(result.status_code, 200 if attempt < 10 else 429, result.content)
        self.assertTrue(AuthThrottle.objects.filter(count=10).exists())
        self.assertFalse(AuthThrottle.objects.filter(key__contains="no-account").exists())

    @override_settings(ACCOUNT_TRUSTED_PROXY_NETWORKS=())
    def test_resend_mail_budget_does_not_allow_unbounded_delivery_to_same_address(self):
        created = self.register()
        self.assertEqual(created.status_code, 302, created.content)
        for attempt in range(7):
            client = Client(enforce_csrf_checks=True)
            address = "NEW-ACCOUNT@EXAMPLE.INVALID" if attempt % 2 else "new-account@example.invalid"
            response = self.form("resend_verification", {"username": "new-account", "email": address}, client=client,
                                 REMOTE_ADDR=f"198.51.100.{attempt + 1}")
            self.assertEqual(response.status_code, 302, response.content)
            self.assertEqual(response["Location"], reverse("verification_sent"))
        self.assertEqual(len(mail.outbox), 5)
        self.assertEqual(self.users.filter(username="new-account").count(), 1)


class ConcurrentEmailConfirmationTests(TransactionTestCase):
    def test_only_one_pending_account_can_confirm_the_same_email_concurrently(self):
        users = get_user_model().objects
        address = "race@example.invalid"
        digests = []
        for prefix in ("first", "second"):
            user = users.create_user(
                username=f"email-confirm-{prefix}", email=address, password=PASSWORD,
                is_active=False, registration_pending=True,
            )
            digest = token_digest(prefix[0] * 43)
            EmailVerification.objects.create(
                user=user, target_email=address, token_digest=digest,
                expires_at=timezone.now() + timedelta(hours=1),
            )
            digests.append(digest)
        barrier = Barrier(2, timeout=8)

        def confirm(digest):
            connection = connections["default"]
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                barrier.wait()
                return confirm_verification(digest, password=PASSWORD)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm, digest) for digest in digests]
            results = [future.result(timeout=16) for future in futures]
        self.assertCountEqual(results, ["verified", "invalid"])
        self.assertEqual(users.filter(email__iexact=address, email_verified_at__isnull=False).count(), 1)
        self.assertEqual(users.filter(email__iexact=address, is_active=True).count(), 1)
