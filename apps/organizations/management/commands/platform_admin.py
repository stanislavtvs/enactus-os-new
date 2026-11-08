from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.platform import record_platform
from apps.organizations.platform_access import PLATFORM_ADMIN_GROUP


class Command(BaseCommand):
    help = "Assign or remove explicit platform management access for an existing account."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "grant", "revoke"])
        parser.add_argument("username", nargs="?")
        parser.add_argument("--confirm", action="store_true", help="Confirm this administrative change.")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["action"] == "list":
            if options["username"] or options["confirm"]:
                raise CommandError("The list action takes no username or confirmation.")
            administrators = get_user_model().objects.filter(groups__name=PLATFORM_ADMIN_GROUP).order_by("username")
            for username, active in administrators.values_list("username", "is_active"):
                self.stdout.write(f"{username} {'active' if active else 'inactive'}")
            return
        if not options["username"]:
            raise CommandError("Specify an account username.")
        if not options["confirm"]:
            raise CommandError("Specify --confirm to apply this change.")
        user = get_user_model().objects.select_for_update().filter(username=options["username"]).first()
        if user is None:
            raise CommandError("Account not found.")
        grant = options["action"] == "grant"
        if grant and not user.is_active:
            raise CommandError("Platform management requires an active account.")
        group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        if user.groups.filter(pk=group.pk).exists() == grant:
            self.stdout.write("Platform access already has the requested state.")
            return
        if grant:
            user.groups.add(group)
        else:
            user.groups.remove(group)
        record_platform(
            actor=None, action="platform.admin_granted" if grant else "platform.admin_revoked",
            subject_id=user.pk, metadata={"user_id": user.pk},
        )
        self.stdout.write(self.style.SUCCESS("Platform access updated."))
