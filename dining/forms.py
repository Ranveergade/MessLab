"""Forms for recording waste and closing a service day."""
from __future__ import annotations

from django import forms

from menu.models import Dish, MealSlot


class WasteEntryForm(forms.Form):
    date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "input", "type": "date"})
    )
    meal = forms.ModelChoiceField(
        queryset=MealSlot.objects.all(),
        empty_label=None,
        widget=forms.Select(attrs={"class": "select"}),
    )
    dish = forms.ModelChoiceField(
        queryset=Dish.objects.filter(is_active=True).order_by("name"),
        empty_label=None,
        widget=forms.Select(attrs={"class": "select"}),
    )
    prepared_kg = forms.DecimalField(
        min_value=0, max_digits=8, decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "input", "min": 0, "step": "0.01", "placeholder": "20.00"}
        ),
    )
    consumed_kg = forms.DecimalField(
        min_value=0, max_digits=8, decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "input", "min": 0, "step": "0.01", "placeholder": "17.50"}
        ),
    )
    wasted_kg = forms.DecimalField(
        min_value=0, max_digits=8, decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "input", "min": 0, "step": "0.01", "placeholder": "2.50"}
        ),
    )
    leftover_reusable = forms.BooleanField(required=False)

    def clean(self):
        cleaned = super().clean()
        prepared = cleaned.get("prepared_kg")
        consumed = cleaned.get("consumed_kg")
        wasted = cleaned.get("wasted_kg")
        if prepared is None or consumed is None or wasted is None:
            return cleaned
        if wasted > prepared:
            self.add_error("wasted_kg", "Wasted kg cannot exceed prepared kg.")
        if consumed > prepared:
            self.add_error("consumed_kg", "Consumed kg cannot exceed prepared kg.")
        if consumed + wasted > prepared:
            self.add_error(
                "consumed_kg",
                "Consumed plus wasted kg cannot exceed prepared kg.",
            )
        return cleaned
