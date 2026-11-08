from django import forms

from .models import Organization

MAX_ID = 2**63 - 1


class PolicyForm(forms.Form):
    policy = forms.ChoiceField(label="Способ вступления", choices=Organization.JoinPolicy.choices)


class InviteForm(forms.Form):
    username = forms.CharField(label="Имя пользователя", max_length=150)


class RevokeForm(forms.Form):
    invitation_id = forms.IntegerField(min_value=1, max_value=MAX_ID)


class ReviewForm(forms.Form):
    request_id = forms.IntegerField(min_value=1, max_value=MAX_ID)
    decision = forms.ChoiceField(choices=[("approve", "Одобрить"), ("reject", "Отклонить")])


class ManagerForm(forms.Form):
    membership_id = forms.IntegerField(min_value=1, max_value=MAX_ID)
    enabled = forms.ChoiceField(choices=[("yes", "Разрешить"), ("no", "Отозвать")])


class InvitationResponseForm(forms.Form):
    invitation_id = forms.IntegerField(required=False, min_value=1, max_value=MAX_ID)
    token = forms.RegexField(r"\A[A-Za-z0-9_-]{43}\Z", required=False, max_length=43)
    decision = forms.ChoiceField(choices=[("accept", "Принять"), ("decline", "Отклонить")])

    def clean(self):
        data = super().clean()
        if bool(data.get("invitation_id")) == bool(data.get("token")):
            raise forms.ValidationError("Укажите одно приглашение.")
        return data
