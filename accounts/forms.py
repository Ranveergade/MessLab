"""Account forms shared by the web views and the API."""
from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator

from .models import StudentProfile, User
from .registration import SELF_REGISTER_ROLES

PHONE_VALIDATOR = RegexValidator(
    r"^\+?[0-9][0-9 \-]{6,19}$",
    "Enter a phone number using digits, spaces or dashes (7–20 characters).",
)


class RegistrationForm(forms.Form):
    full_name = forms.CharField(
        max_length=150,
        strip=True,
        widget=forms.TextInput(
            attrs={"class": "input", "autocomplete": "name", "placeholder": "Asha Verma"}
        ),
    )
    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "class": "input",
                "autocomplete": "email",
                "autocapitalize": "none",
                "spellcheck": "false",
                "placeholder": "you@college.edu",
            }
        ),
    )
    role = forms.ChoiceField(
        choices=[(role, User.Role(role).label) for role in SELF_REGISTER_ROLES],
        widget=forms.Select(attrs={"class": "select", "data-reg-role": ""}),
    )
    hostel = forms.ChoiceField(
        choices=StudentProfile.Hostel.choices,
        required=False,
        widget=forms.Select(attrs={"class": "select", "data-reg-hostel": ""}),
    )
    diet = forms.ChoiceField(
        choices=StudentProfile.Diet.choices,
        required=False,
        initial=StudentProfile.Diet.VEG,
        widget=forms.Select(attrs={"class": "select"}),
    )
    phone = forms.CharField(
        max_length=20,
        required=False,
        validators=[PHONE_VALIDATOR],
        widget=forms.TextInput(
            attrs={"class": "input", "autocomplete": "tel", "placeholder": "+91 90000 00000"}
        ),
    )
    consent = forms.BooleanField(
        required=True,
        error_messages={
            "required": "Please accept the privacy notice to create an account."
        },
    )

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        hostel = cleaned.get("hostel") or None
        if role == User.Role.HOSTEL_STUDENT and not hostel:
            self.add_error("hostel", "Choose your hostel.")
        if role != User.Role.HOSTEL_STUDENT:
            cleaned["hostel"] = None
        cleaned["diet"] = cleaned.get("diet") or StudentProfile.Diet.VEG
        cleaned["phone"] = (cleaned.get("phone") or "").strip()
        return cleaned


class RegistrationVerifyForm(forms.Form):
    email = forms.EmailField(max_length=254)
    code = forms.RegexField(r"^\d{6}$", error_messages={
        "invalid": "Enter the 6-digit code from your email."
    })

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()
