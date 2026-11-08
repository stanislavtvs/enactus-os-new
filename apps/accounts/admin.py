from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import transaction

from .models import EmailVerification, User


@admin.register(User)
class AccountAdmin(UserAdmin):
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
