from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models import ProtectedError, QuerySet
from django.http import HttpResponseRedirect

from apps.organizations.models import Organization

from .models import EmailVerification, User


OWNER_ACCOUNT_CONSTRAINT = "organization_owner_account_protected"
OWNER_ACCOUNT_ERROR = (
    "Аккаунт владельца защищён. Сначала передайте владение всеми его организациями."
)


def _owner_integrity_error(error):
    return getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == OWNER_ACCOUNT_CONSTRAINT


def _owner_delete_error(error):
    return isinstance(error, ProtectedError) and any(
        isinstance(item, Organization) for item in error.protected_objects
    )


class AccountChangeForm(UserChangeForm):
    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        if (
            self.instance.pk and "is_active" in self.changed_data
            and Organization.objects.filter(owner_id=self.instance.pk).exists()
        ):
            message = (
                "Для повторного включения аккаунта владельца используйте процедуру восстановления."
                if active else OWNER_ACCOUNT_ERROR
            )
            raise ValidationError(message, code=OWNER_ACCOUNT_CONSTRAINT)
        return active


@admin.register(User)
class AccountAdmin(UserAdmin):
    form = AccountChangeForm
    actions = ("disable_selected", "delete_selected")

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except IntegrityError as exc:
            if not _owner_integrity_error(exc):
                raise
            self.message_user(request, OWNER_ACCOUNT_ERROR, messages.ERROR)
            return HttpResponseRedirect(request.path)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if change:
            current = User.objects.select_for_update().get(pk=obj.pk)
            obj.registration_pending = current.registration_pending
            if "is_active" not in form.changed_data:
                obj.is_active = current.is_active
            if "email" in form.changed_data:
                obj.email_verified_at = None
                EmailVerification.objects.filter(user=obj).delete()
            else:
                obj.email = current.email
                obj.email_verified_at = current.email_verified_at
        super().save_model(request, obj, form, change)

    @admin.action(permissions=["change"], description="Отключить выбранные аккаунты")
    def disable_selected(self, request, queryset):
        db = router.db_for_write(self.model)
        active = queryset.using(db).filter(is_active=True)
        try:
            with transaction.atomic(using=db):
                if Organization.objects.using(db).filter(owner_id__in=active.values("pk")).exists():
                    self.message_user(request, OWNER_ACCOUNT_ERROR, messages.ERROR)
                    return
                count = active.update(is_active=False)
        except IntegrityError as exc:
            if not _owner_integrity_error(exc):
                raise
            self.message_user(request, OWNER_ACCOUNT_ERROR, messages.ERROR)
            return
        self.message_user(request, f"Отключено аккаунтов: {count}.", messages.SUCCESS)

    def get_deleted_objects(self, objs, request):
        deleted, count, permissions, protected = super().get_deleted_objects(objs, request)
        account_ids = objs.values("pk") if isinstance(objs, QuerySet) else [obj.pk for obj in objs]
        if Organization.objects.filter(owner_id__in=account_ids).exists():
            protected.append(OWNER_ACCOUNT_ERROR)
        return deleted, count, permissions, protected

    def delete_view(self, request, object_id, extra_context=None):
        try:
            return super().delete_view(request, object_id, extra_context)
        except IntegrityError as exc:
            if not (_owner_delete_error(exc) or _owner_integrity_error(exc)):
                raise
            self.message_user(request, OWNER_ACCOUNT_ERROR, messages.ERROR)
            return HttpResponseRedirect(request.path)

    @admin.action(permissions=["delete"], description=django_delete_selected.short_description)
    def delete_selected(self, request, queryset):
        try:
            with transaction.atomic(using=router.db_for_write(self.model)):
                return django_delete_selected(self, request, queryset)
        except IntegrityError as exc:
            if not (_owner_delete_error(exc) or _owner_integrity_error(exc)):
                raise
            self.message_user(request, OWNER_ACCOUNT_ERROR, messages.ERROR)
