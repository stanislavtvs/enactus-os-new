from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.calendar_app.invitation_models import CalendarInvitation
from apps.calendar_app.models import CalendarEvent
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import create_organization
from apps.projects.models import Project, ProjectMembership
from apps.tasks.models import Task


class OrganizationDashboardTests(TestCase):
    current = datetime(2030, 11, 8, 10, tzinfo=timezone.utc)

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_superuser(username="dashboard-owner")
        cls.member = users.create_user(username="dashboard-member")
        cls.org = create_organization(user=cls.owner, name="Dashboard workspace")
        cls.other = create_organization(user=cls.owner, name="Other workspace")
        cls.owner_member = cls.org.memberships.get(user=cls.owner)
        cls.member_record = OrganizationMembership.objects.create(organization=cls.org, user=cls.member)
        cls.visible_project = Project.objects.create(
            organization=cls.org, created_by=cls.owner, name="Visible active project", status=Project.Status.ACTIVE,
        )
        ProjectMembership.objects.create(
            organization=cls.org, project=cls.visible_project, organization_membership=cls.member_record,
        )
        cls.hidden_project = Project.objects.create(
            organization=cls.org, created_by=cls.owner, name="Hidden active project", status=Project.Status.ACTIVE,
        )
        Project.objects.create(
            organization=cls.other, created_by=cls.owner, name="Foreign active project", status=Project.Status.ACTIVE,
        )
        cls.overdue = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, assignee=cls.member_record,
            name="My overdue task", due_at=cls.current - timedelta(hours=1),
        )
        cls.upcoming = Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, assignee=cls.member_record,
            name="<My upcoming task>", due_at=cls.current + timedelta(days=1),
        )
        Task.objects.create(
            organization=cls.org, created_by=cls.owner_member, project=cls.hidden_project,
            name="Hidden task deadline", due_at=cls.current + timedelta(hours=1),
        )
        for title in ["Invited dashboard meeting", "Hidden dashboard meeting"]:
            event = CalendarEvent.objects.create(
                organization=cls.org, created_by=cls.owner_member, event_type=CalendarEvent.Type.MEETING,
                title=title, starts_at=cls.current + timedelta(days=2),
                ends_at=cls.current + timedelta(days=2, hours=1), timezone="UTC",
            )
            if title.startswith("Invited"):
                CalendarInvitation.objects.create(
                    organization=cls.org, event=event, recipient=cls.member_record, invited_by=cls.owner_member,
                )

    def overview(self, organization=None):
        with patch("apps.organizations.dashboard.now", return_value=self.current):
            return self.client.get(reverse("organization-detail", args=[(organization or self.org).pk]))

    def test_member_dashboard_reuses_project_task_and_calendar_visibility(self):
        self.client.force_login(self.member)
        response = self.overview()
        self.assertContains(response, "Visible active project")
        self.assertContains(response, "&lt;My upcoming task&gt;")
        self.assertContains(response, "My overdue task")
        self.assertContains(response, "Invited dashboard meeting")
        self.assertContains(response, reverse("organization-project", args=[self.org.pk, self.visible_project.pk]))
        self.assertContains(response, reverse("organization-task", args=[self.org.pk, self.upcoming.pk]))
        self.assertNotContains(response, "Hidden active project")
        self.assertNotContains(response, "Foreign active project")
        self.assertNotContains(response, "Hidden task deadline")
        self.assertNotContains(response, "Hidden dashboard meeting")
        self.assertEqual(response.context["project_count"], 1)
        self.assertEqual(response.context["attention_tasks"], [self.overdue])
        self.assertEqual(response.context["upcoming_deadlines"], [self.upcoming])
        self.assertEqual(self.overview(self.other).status_code, 404)

    def test_previews_are_bounded_and_omit_closed_tasks(self):
        for index in range(6):
            Task.objects.create(
                organization=self.org, created_by=self.owner_member, assignee=self.member_record,
                name=f"Another personal task {index}", due_at=self.current + timedelta(days=2, hours=index),
            )
        Task.objects.create(
            organization=self.org, created_by=self.owner_member, assignee=self.member_record,
            name="Closed task deadline", due_at=self.current + timedelta(hours=1), status=Task.Status.CANCELLED,
        )
        self.client.force_login(self.member)
        response = self.overview()
        self.assertEqual(len(response.context["personal_tasks"]), 5)
        self.assertEqual(len(response.context["upcoming_deadlines"]), 5)
        self.assertNotContains(response, "Another personal task 5")
        self.assertNotContains(response, "Closed task deadline")
