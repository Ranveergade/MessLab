"""Forms for the menu-management page."""
from __future__ import annotations

from django import forms

from .models import Dish, MealSlot
from .services import dish_name_exists


class DishForm(forms.ModelForm):
    class Meta:
        model = Dish
        fields = (
            "name",
            "category",
            "diet_type",
            "avg_portion_grams",
            "popularity_baseline",
            "is_active",
        )
        widgets = {
            "name": forms.TextInput(attrs={"class": "input", "placeholder": "Paneer butter masala"}),
            "category": forms.Select(attrs={"class": "select"}),
            "diet_type": forms.Select(attrs={"class": "select"}),
            "avg_portion_grams": forms.NumberInput(
                attrs={"class": "input", "min": 20, "max": 900, "step": 10}
            ),
            "popularity_baseline": forms.NumberInput(
                attrs={"class": "input", "min": 0, "max": 1, "step": 0.05}
            ),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data["name"] or "").strip()
        if dish_name_exists(name, exclude_pk=self.instance.pk if self.instance.pk else None):
            raise forms.ValidationError("A dish with that name already exists.")
        return name


class MealSlotForm(forms.ModelForm):
    class Meta:
        model = MealSlot
        fields = (
            "name",
            "start_time",
            "end_time",
            "peak_start",
            "peak_end",
            "counters",
            "counter_capacity_per_hour",
            "order",
        )
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "input", "placeholder": "midnight"}
            ),
            "start_time": forms.TimeInput(attrs={"class": "input", "type": "time"}),
            "end_time": forms.TimeInput(attrs={"class": "input", "type": "time"}),
            "peak_start": forms.TimeInput(attrs={"class": "input", "type": "time"}),
            "peak_end": forms.TimeInput(attrs={"class": "input", "type": "time"}),
            "counters": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "counter_capacity_per_hour": forms.NumberInput(
                attrs={"class": "input", "min": 1, "step": 10}
            ),
            "order": forms.NumberInput(attrs={"class": "input", "min": 0}),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data["name"] or "").strip().lower().replace(" ", "_")
        if not name.replace("_", "").isalnum():
            raise forms.ValidationError("Use letters, digits and underscores only.")
        qs = MealSlot.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A meal slot with that name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        peak_start = cleaned.get("peak_start")
        peak_end = cleaned.get("peak_end")
        if start and end and start >= end:
            self.add_error("end_time", "The end time must be after the start time.")
        if start and peak_start and peak_start < start:
            self.add_error("peak_start", "The peak must start inside the service window.")
        if end and peak_end and peak_end > end:
            self.add_error("peak_end", "The peak must end inside the service window.")
        if peak_start and peak_end and peak_start >= peak_end:
            self.add_error("peak_end", "The peak must end after it starts.")
        return cleaned


class MenuMetaForm(forms.Form):
    date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "input", "type": "date"})
    )
    meal = forms.ModelChoiceField(
        queryset=MealSlot.objects.order_by("order"),
        empty_label=None,
        widget=forms.Select(attrs={"class": "select"}),
    )
    notes = forms.CharField(
        max_length=240,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "Special note for this service"}
        ),
    )
    is_special = forms.BooleanField(required=False)
