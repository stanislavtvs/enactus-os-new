from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView, PasswordResetConfirmView, PasswordResetView
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from . import services, throttle
from .forms import ChangeEmailForm, RegistrationForm, ResendVerificationForm, VerifyRegistrationForm
from .models import User
from .reset_queue import enqueue_password_reset

VERIFICATION_SESSION_KEY = "account_email_verification"
RETRY_MESSAGE = "Слишком много попыток. Повторите позже."


def _render_verification(request, *, row=None, form=None, verified=False, rate_limited=False):
    response = render(request, "registration/verify_email.html", {
        "can_confirm": row is not None,
        "verified": verified,
        "requires_password": row is not None and row.user.registration_pending,
        "form": form,
        "rate_limited": rate_limited,
    })
    response["Referrer-Policy"] = "same-origin"
    return response


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("password1", "password2")
@csrf_protect
@never_cache
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST" and not throttle.allow(
        request, "register", services.normalize_email(request.POST.get("email", "")),
    ):
        return redirect("verification_sent")
    form = RegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        result = services.register_pending(form.save(commit=False))
        if result == "username_taken":
            form.add_error("username", "Это имя пользователя уже занято.")
        else:
            if result == "unavailable":
                request.session["account_verification_unavailable"] = True
            return redirect("verification_sent")
    return render(request, "registration/register.html", {"form": form})


@require_GET
@never_cache
def verification_sent(request):
    return render(request, "registration/verification_sent.html", {
        "delivery_unavailable": request.session.pop("account_verification_unavailable", False),
    })


@require_http_methods(["GET", "POST"])
@csrf_protect
@never_cache
def resend_verification(request):
    form = ResendVerificationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        address = services.normalize_email(form.cleaned_data["email"])
        if throttle.allow(request, "resend", address):
            services.resend_pending(address, form.cleaned_data["username"])
        return redirect("verification_sent")
    return render(request, "registration/resend_verification.html", {"form": form})


@require_GET
@never_cache
def verify_email_token(request, token):
    digest = services.token_digest(token)
    request.session.pop(VERIFICATION_SESSION_KEY, None)
    if throttle.allow(request, "verify", digest) and digest is not None:
        request.session[VERIFICATION_SESSION_KEY] = digest
    response = redirect("verify_email")
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("password")
@csrf_protect
@never_cache
def verify_email(request):
    digest = request.session.get(VERIFICATION_SESSION_KEY)
    actor_id = request.user.pk if request.user.is_authenticated else None
    if request.method == "POST" and not throttle.allow(request, "verify", digest):
        return _render_verification(request, rate_limited=True)
    row = services.pending_verification(digest, actor_id=actor_id)
    if row is None:
        request.session.pop(VERIFICATION_SESSION_KEY, None)
        return _render_verification(request)

    registration = row.user.registration_pending
    form = VerifyRegistrationForm(request.POST if request.method == "POST" else None) if registration else None
    if request.method == "POST" and (form is None or form.is_valid()):
        result = services.confirm_verification(
            digest, actor_id=actor_id, password=form.cleaned_data["password"] if form else "",
        )
        if result == "invalid_password" and form is not None:
            form.add_error("password", "Пароль не совпадает с указанным при регистрации.")
        else:
            request.session.pop(VERIFICATION_SESSION_KEY, None)
            return _render_verification(request, verified=result == "verified")
    return _render_verification(request, row=row, form=form)


@login_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("password")
@csrf_protect
@never_cache
def change_email(request):
    form = ChangeEmailForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        address = services.normalize_email(form.cleaned_data["email"])
        if not throttle.allow(request, "change_email", address):
            form.add_error(None, RETRY_MESSAGE)
        else:
            result = services.request_email_change(
                user_id=request.user.pk, email=address, password=form.cleaned_data["password"],
            )
            if result == "sent":
                return redirect("verification_sent")
            errors = {
                "invalid_password": ("password", "Действующий пароль указан неверно."),
                "email_taken": ("email", "Этот адрес недоступен для подтверждения."),
                "unchanged": ("email", "Этот адрес уже подтверждён."),
                "unavailable": (None, "Письмо сейчас не удалось отправить. Повторите позже."),
            }
            form.add_error(*errors[result])
    return render(request, "registration/change_email.html", {"form": form})


class AccountLoginView(LoginView):
    def post(self, request, *args, **kwargs):
        identity = request.POST.get("username", "").strip().casefold()
        if not throttle.allow(request, "login", identity):
            form = self.form_class(request=request, data={}, empty_permitted=True, use_required_attribute=False)
            form.add_error(None, RETRY_MESSAGE)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)


class AccountPasswordResetView(PasswordResetView):
    def form_valid(self, form):
        address = services.normalize_email(form.cleaned_data["email"])
        if throttle.allow(self.request, "reset", address):
            enqueue_password_reset(address)
        return redirect(self.get_success_url())


@method_decorator(sensitive_post_parameters(), name="dispatch")
@method_decorator(csrf_protect, name="dispatch")
class AccountPasswordResetConfirmView(PasswordResetConfirmView):
    token_generator = services.account_reset_token_generator

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        if not throttle.allow(request, "reset_confirm", kwargs.get("uidb64")):
            self.validlink = False
            response = self.render_to_response(self.get_context_data())
        elif request.method == "POST":
            with transaction.atomic():
                response = super().dispatch(request, *args, **kwargs)
        else:
            response = super().dispatch(request, *args, **kwargs)
        # The token URL only redirects. The clean page needs a same-origin
        # referrer policy so browsers send an Origin usable by CSRF checks.
        response["Referrer-Policy"] = (
            "same-origin" if kwargs.get("token") == self.reset_url_token else "no-referrer"
        )
        return response

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        if user is not None and self.request.method == "POST":
            user = User.objects.select_for_update().filter(pk=user.pk).first()
        return user if services.eligible_for_reset(user) else None

    def form_valid(self, form):
        response = super().form_valid(form)
        services.invalidate_pending_email(self.user)
        return response


class AccountPasswordChangeView(PasswordChangeView):
    def post(self, request, *args, **kwargs):
        if not throttle.allow(request, "password_change", str(request.user.pk)):
            form = self.form_class(user=request.user, data={}, empty_permitted=True, use_required_attribute=False)
            form.add_error(None, RETRY_MESSAGE)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        with transaction.atomic():
            user = User.objects.select_for_update().filter(pk=request.user.pk, is_active=True).first()
            if user is None or not constant_time_compare(
                user.get_session_auth_hash(), request.session.get(HASH_SESSION_KEY, ""),
            ):
                return redirect("login")
            request.user = user
            return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        services.invalidate_pending_email(self.request.user)
        return response
