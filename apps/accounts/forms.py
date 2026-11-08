from django import forms
from django.contrib.auth.forms import UserCreationForm, UsernameField

from .models import User
from .services import normalize_email


def email_field():
    return forms.EmailField(
        label="Электронная почта", max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )


def password_field(*, label="Действующий пароль"):
    return forms.CharField(
        label=label, strip=False, max_length=4096,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class RegistrationForm(UserCreationForm):
    email = email_field()

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")
        widgets = {"username": forms.TextInput(attrs={"autocomplete": "username"})}

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])


class ChangeEmailForm(forms.Form):
    email = email_field()
    password = password_field()


class ResendVerificationForm(forms.Form):
    username = UsernameField(
        label="Имя пользователя", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )
    email = email_field()


class VerifyRegistrationForm(forms.Form):
    password = password_field(label="Пароль, указанный при регистрации")
