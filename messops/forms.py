"""Forms for the Mess Committee SOP workspace."""
from __future__ import annotations

from django import forms
from django.utils import timezone

from accounts.models import User

from .models import (
    CommitteeMember,
    Complaint,
    MenuChangeRequest,
    MenuPreference,
    ReviewMeeting,
)


class CommitteeMemberForm(forms.ModelForm):
    class Meta:
        model = CommitteeMember
        fields = (
            "user",
            "full_name",
            "email",
            "program",
            "role",
            "term_start",
            "term_end",
            "is_active",
        )
        widgets = {
            "user": forms.Select(attrs={"class": "select"}),
            "full_name": forms.TextInput(
                attrs={"class": "input", "placeholder": "Representative name"}
            ),
            "email": forms.EmailInput(
                attrs={"class": "input", "placeholder": "member@example.edu"}
            ),
            "program": forms.Select(attrs={"class": "select"}),
            "role": forms.Select(attrs={"class": "select"}),
            "term_start": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "term_end": forms.DateInput(attrs={"class": "input", "type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True).order_by(
            "username"
        )
        self.fields["user"].required = False
        self.fields["user"].empty_label = "— not linked to an account —"
        if not self.initial.get("term_start") and not self.instance.pk:
            self.initial["term_start"] = timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("full_name") and not cleaned.get("user"):
            self.add_error(
                "full_name", "Add a name or link a MessLab account for this member."
            )
        return cleaned


class MenuPreferenceForm(forms.ModelForm):
    class Meta:
        model = MenuPreference
        fields = ("meal", "preference", "notes")
        widgets = {
            "meal": forms.Select(attrs={"class": "select"}),
            "preference": forms.Textarea(
                attrs={
                    "class": "input",
                    "rows": 4,
                    "placeholder": "e.g. Add more seasonal vegetables; keep Friday biryani.",
                }
            ),
            "notes": forms.TextInput(
                attrs={"class": "input", "placeholder": "Optional note for the committee"}
            ),
        }
        labels = {
            "meal": "Meal (optional)",
            "preference": "Your preference",
        }


class ComplaintForm(forms.ModelForm):
    class Meta:
        model = Complaint
        fields = ("category", "service_date", "meal", "description")
        widgets = {
            "category": forms.Select(attrs={"class": "select"}),
            "service_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "meal": forms.Select(attrs={"class": "select"}),
            "description": forms.Textarea(
                attrs={
                    "class": "input",
                    "rows": 5,
                    "placeholder": (
                        "What happened? Include the dish, meal and time you noticed it."
                    ),
                }
            ),
        }
        labels = {"service_date": "Meal date (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["meal"].required = False
        self.fields["meal"].empty_label = "— all meals —"
        if not self.initial.get("service_date") and not self.instance.pk:
            self.initial["service_date"] = timezone.localdate()

    def clean_description(self) -> str:
        value = (self.cleaned_data.get("description") or "").strip()
        if len(value) < 10:
            raise forms.ValidationError("Please describe the issue in a little more detail.")
        return value


class ReviewMeetingForm(forms.ModelForm):
    class Meta:
        model = ReviewMeeting
        fields = (
            "meeting_date",
            "attendees",
            "agenda",
            "minutes",
            "actions",
            "escalated",
        )
        widgets = {
            "meeting_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "attendees": forms.Textarea(
                attrs={
                    "class": "input",
                    "rows": 2,
                    "placeholder": "All four representatives and the mess operator",
                }
            ),
            "agenda": forms.Textarea(attrs={"class": "input", "rows": 4}),
            "minutes": forms.Textarea(attrs={"class": "input", "rows": 4}),
            "actions": forms.Textarea(attrs={"class": "input", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("meeting_date"):
            self.initial["meeting_date"] = timezone.localdate()
        if not self.initial.get("agenda"):
            self.initial["agenda"] = ReviewMeeting.DEFAULT_AGENDA


class MenuChangeForm(forms.ModelForm):
    class Meta:
        model = MenuChangeRequest
        fields = ("service_date", "meal", "summary", "reason")
        widgets = {
            "service_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "meal": forms.Select(attrs={"class": "select"}),
            "summary": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Replace paneer with soya curry"}
            ),
            "reason": forms.Textarea(
                attrs={
                    "class": "input",
                    "rows": 3,
                    "placeholder": "Ingredient shortage, operational difficulty, etc.",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["meal"].required = False
        self.fields["meal"].empty_label = "— all meals —"
        if not self.initial.get("service_date") and not self.instance.pk:
            self.initial["service_date"] = timezone.localdate()
