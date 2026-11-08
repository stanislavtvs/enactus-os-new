from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse


class HealthTests(SimpleTestCase):
    @override_settings(SECRET_KEY="test-secret-never-returned-by-health")
    def test_health_is_public_and_returns_only_status(self):
        response = self.client.get("/api/v1/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotContains(response, settings.SECRET_KEY)


class SessionTests(TestCase):
    password = "local-only-test-password"

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="smoke-user",
            password=cls.password,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.home_url = reverse("home")
        self.login_url = reverse("login")
        self.logout_url = reverse("logout")

    def csrf_token(self):
        self.client.get(self.login_url)
        return self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def login_data(self, password):
        return {
            "username": self.user.username,
            "password": password,
            "csrfmiddlewaretoken": self.csrf_token(),
        }

    def test_home_redirects_anonymous_user_to_login(self):
        self.assertRedirects(
            self.client.get(self.home_url),
            f"{self.login_url}?next={self.home_url}",
        )

    def test_login_and_logout_update_session(self):
        response = self.client.post(self.login_url, self.login_data(self.password))

        self.assertRedirects(response, self.home_url)
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["httponly"])
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertEqual(self.client.get(self.home_url).status_code, 200)

        # Django rotates the CSRF token on login.
        response = self.client.post(
            self.logout_url,
            {"csrfmiddlewaretoken": self.client.cookies[settings.CSRF_COOKIE_NAME].value},
        )

        self.assertRedirects(response, self.login_url)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertRedirects(
            self.client.get(self.home_url),
            f"{self.login_url}?next={self.home_url}",
        )

    def test_wrong_password_does_not_start_session(self):
        response = self.client.post(self.login_url, self.login_data("wrong-password"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_login_rejects_post_without_csrf_token(self):
        data = self.login_data(self.password)
        del data["csrfmiddlewaretoken"]

        self.assertEqual(self.client.post(self.login_url, data).status_code, 403)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_logout_requires_post_with_csrf_token(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(self.logout_url).status_code, 405)
        self.assertEqual(self.client.post(self.logout_url).status_code, 403)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))

    def test_login_ignores_external_redirect(self):
        data = self.login_data(self.password)
        data["next"] = "https://example.invalid/"

        self.assertRedirects(self.client.post(self.login_url, data), self.home_url)
