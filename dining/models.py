"""Dining operations: intents, check-ins, waste, forecasts and preparation plans."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from menu.models import Dish, MealSlot


class MealIntent(models.Model):
    class Status(models.TextChoices):
        YES = "YES", "Coming"
        NO = "NO", "Skipping"
        MAYBE = "MAYBE", "Maybe"

    class Source(models.TextChoices):
        DEFAULT = "default", "Weekly default"
        ONE_TAP = "one_tap", "One-tap"
        AUTO_INFERRED = "auto-inferred", "Auto-inferred from habits"

    class Arrival(models.TextChoices):
        EARLY = "early", "Early (first 30 min)"
        PEAK = "peak", "Peak window"
        LATE = "late", "Late (last 45 min)"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meal_intents"
    )
    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.PROTECT, related_name="intents")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.YES)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.ONE_TAP)
    expected_arrival_window = models.CharField(
        max_length=8, choices=Arrival.choices, default=Arrival.PEAK
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "meal__order", "student_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "date", "meal"], name="uniq_intent_student_date_meal"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["YES", "NO", "MAYBE"]), name="intent_status_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["date", "meal", "status"], name="intent_date_meal_status_idx"),
            models.Index(fields=["student", "date"], name="intent_student_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.date} {self.meal.name}: {self.status}"

    def clean(self) -> None:
        role = getattr(self.student, "effective_role", getattr(self.student, "role", ""))
        if self.student_id and role not in {"student", "faculty"}:
            raise ValidationError("Only students and faculty can declare meal intent.")

    @property
    def is_positive(self) -> bool:
        return self.status in {self.Status.YES, self.Status.MAYBE}


class MealFeedback(models.Model):
    """A student's answer to "should the mess make this menu?"."""

    class Response(models.TextChoices):
        YES = "yes", "Yes, make this"
        NO = "no", "No, change it"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meal_feedback",
    )
    date = models.DateField(db_index=True)
    meal = models.ForeignKey(
        MealSlot, on_delete=models.PROTECT, related_name="feedback"
    )
    response = models.CharField(max_length=4, choices=Response.choices)
    suggestion = models.CharField(
        max_length=240,
        blank=True,
        help_text="What should be made instead, or what to keep.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "meal__order", "student_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "date", "meal"],
                name="uniq_feedback_student_date_meal",
            ),
            models.CheckConstraint(
                condition=Q(response__in=["yes", "no"]),
                name="feedback_response_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["date", "meal", "response"],
                name="feedback_date_meal_resp_idx",
            ),
            models.Index(
                fields=["student", "date"], name="feedback_student_date_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.date} {self.meal.name}: {self.response}"


class CheckIn(models.Model):
    class Source(models.TextChoices):
        QR = "qr", "QR scan"
        ID_TAP = "id_tap", "ID card tap"
        MANUAL = "manual", "Manual entry"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="check_ins"
    )
    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.PROTECT, related_name="check_ins")
    timestamp = models.DateTimeField(default=timezone.now)
    dishes_taken = models.ManyToManyField(
        Dish, through="CheckInDish", related_name="check_ins", blank=True
    )
    clean_plate = models.BooleanField(default=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.QR)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "date", "meal"], name="uniq_checkin_student_date_meal"
            )
        ]
        indexes = [
            models.Index(fields=["date", "meal", "timestamp"], name="checkin_date_meal_ts_idx"),
            models.Index(fields=["student", "date"], name="checkin_student_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} @ {self.meal.name} {self.date}"

    def clean(self) -> None:
        if self.timestamp and timezone.is_naive(self.timestamp):
            raise ValidationError("Check-in timestamps must be timezone-aware.")


class CheckInDish(models.Model):
    check_in = models.ForeignKey(CheckIn, on_delete=models.CASCADE, related_name="dish_lines")
    dish = models.ForeignKey(Dish, on_delete=models.PROTECT, related_name="checkin_lines")
    portions = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["check_in", "dish"], name="uniq_checkin_dish"),
            models.CheckConstraint(
                condition=Q(portions__gte=1), name="checkin_dish_portions_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.dish.name} x{self.portions}"


class DayContext(models.Model):
    """Calendar / weather context used by the forecast for future dates too."""

    class Weather(models.TextChoices):
        CLEAR = "clear", "Clear"
        RAIN = "rain", "Rainy"
        HOT = "hot", "Hot"
        COLD = "cold", "Cold"

    date = models.DateField(unique=True, db_index=True)
    is_holiday = models.BooleanField(default=False)
    is_exam = models.BooleanField(default=False)
    weather = models.CharField(max_length=8, choices=Weather.choices, default=Weather.CLEAR)
    event_name = models.CharField(max_length=80, blank=True)
    is_special_menu = models.BooleanField(default=False)
    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["date"]
        indexes = [models.Index(fields=["date"], name="daycontext_date_idx")]

    def __str__(self) -> str:
        return f"{self.date} ({self.get_weather_display()})"


class MealService(models.Model):
    """Operational record of what actually happened at a meal.

    This is the training spine for the participation model. ``declared_intents``
    is the number of intents known at the planning cutoff, not the final count.
    """

    class Weather(models.TextChoices):
        CLEAR = "clear", "Clear"
        RAIN = "rain", "Rainy"
        HOT = "hot", "Hot"
        COLD = "cold", "Cold"

    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.PROTECT, related_name="services")
    actual_participants = models.PositiveIntegerField(default=0)
    declared_intents = models.PositiveIntegerField(default=0)
    weather = models.CharField(max_length=8, choices=Weather.choices, default=Weather.CLEAR)
    is_holiday = models.BooleanField(default=False)
    is_exam = models.BooleanField(default=False)
    is_special_menu = models.BooleanField(default=False)
    event_name = models.CharField(max_length=80, blank=True)
    notes = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "meal__order"]
        constraints = [
            models.UniqueConstraint(fields=["date", "meal"], name="uniq_service_date_meal"),
            models.CheckConstraint(
                condition=Q(declared_intents__gte=0), name="service_intents_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["date", "meal"], name="service_date_meal_idx"),
            models.Index(fields=["is_exam", "is_holiday"], name="service_flags_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.meal.name}: {self.actual_participants} ate"


class WasteLog(models.Model):
    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.PROTECT, related_name="waste_logs")
    dish = models.ForeignKey(Dish, on_delete=models.PROTECT, related_name="waste_logs")
    prepared_kg = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    consumed_kg = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    wasted_kg = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    leftover_reusable = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "meal__order", "dish__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "meal", "dish"], name="uniq_waste_date_meal_dish"
            ),
            models.CheckConstraint(
                condition=Q(wasted_kg__lte=F("prepared_kg")), name="waste_lte_prepared"
            ),
            models.CheckConstraint(
                condition=Q(consumed_kg__lte=F("prepared_kg")), name="consumed_lte_prepared"
            ),
            models.CheckConstraint(condition=Q(prepared_kg__gte=0), name="waste_prepared_non_neg"),
            models.CheckConstraint(condition=Q(consumed_kg__gte=0), name="waste_consumed_non_neg"),
            models.CheckConstraint(condition=Q(wasted_kg__gte=0), name="waste_wasted_non_neg"),
            models.CheckConstraint(
                condition=Q(consumed_kg__lte=F("prepared_kg") - F("wasted_kg")),
                name="waste_consumed_plus_wasted_lte_prepared",
            ),
        ]
        indexes = [
            models.Index(fields=["date", "meal"], name="waste_date_meal_idx"),
            models.Index(fields=["dish", "date"], name="waste_dish_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.meal.name} {self.dish.name}: {self.wasted_kg}kg wasted"

    def clean(self) -> None:
        if self.consumed_kg is not None and self.prepared_kg is not None:
            if self.consumed_kg > self.prepared_kg:
                raise ValidationError("Consumed kg cannot exceed prepared kg.")
        if self.wasted_kg is not None and self.prepared_kg is not None:
            if self.wasted_kg > self.prepared_kg:
                raise ValidationError("Wasted kg cannot exceed prepared kg.")
        if self.consumed_kg is not None and self.wasted_kg is not None:
            if self.consumed_kg + self.wasted_kg > self.prepared_kg + Decimal("0.01"):
                raise ValidationError(
                    "Consumed + wasted cannot exceed prepared (leftovers must be recorded as reusable)."
                )

    @property
    def waste_pct(self) -> float:
        if not self.prepared_kg:
            return 0.0
        return float(self.wasted_kg) / float(self.prepared_kg) * 100


class Forecast(models.Model):
    """Meal-level forecast when dish is NULL; dish-level forecast otherwise."""

    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.CASCADE, related_name="forecasts")
    dish = models.ForeignKey(
        Dish, on_delete=models.CASCADE, related_name="forecasts", null=True, blank=True
    )
    predicted_participation = models.PositiveIntegerField(default=0)
    predicted_demand_kg = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    lower_bound_kg = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    upper_bound_kg = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    lower_participation = models.PositiveIntegerField(default=0)
    upper_participation = models.PositiveIntegerField(default=0)
    model_version = models.CharField(max_length=64, default="untrained")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "meal__order", "dish__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "meal", "dish"], name="uniq_forecast_date_meal_dish"
            ),
            models.UniqueConstraint(
                fields=["date", "meal"],
                condition=Q(dish__isnull=True),
                name="uniq_meal_forecast_date_meal",
            ),
            models.CheckConstraint(
                condition=Q(predicted_demand_kg__gte=0), name="forecast_demand_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(lower_bound_kg__lte=F("upper_bound_kg")), name="forecast_bounds_ordered"
            ),
            models.CheckConstraint(
                condition=Q(lower_participation__lte=F("upper_participation")),
                name="forecast_participation_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["date", "meal"], name="forecast_date_meal_idx"),
            models.Index(fields=["dish", "date"], name="forecast_dish_date_idx"),
        ]

    def __str__(self) -> str:
        label = self.dish.name if self.dish else "meal"
        return f"{self.date} {self.meal.name} {label}: {self.predicted_demand_kg}kg"


class PreparationPlan(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        OVERRIDDEN = "overridden", "Overridden"

    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.CASCADE, related_name="preparation_plans")
    dish = models.ForeignKey(Dish, on_delete=models.PROTECT, related_name="preparation_plans")
    recommended_kg = models.DecimalField(
        max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(Decimal("0"))]
    )
    actual_prepared_kg = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    baseline_kg = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0,
        help_text="What the mess would traditionally cook without MessLab.",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_plans",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "meal__order", "dish__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "meal", "dish"], name="uniq_plan_date_meal_dish"
            ),
            models.CheckConstraint(
                condition=Q(recommended_kg__gte=0), name="plan_recommended_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["date", "meal"], name="plan_date_meal_idx"),
            models.Index(fields=["status", "date"], name="plan_status_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.meal.name} {self.dish.name}: {self.recommended_kg}kg"

    @property
    def planned_waste_avoided_kg(self) -> float:
        return max(0.0, float(self.baseline_kg) - float(self.recommended_kg))

    @property
    def is_approved(self) -> bool:
        return self.status in {self.Status.APPROVED, self.Status.OVERRIDDEN}


class PlanDecisionLog(models.Model):
    class Action(models.TextChoices):
        APPROVE = "approve", "Approved"
        OVERRIDE = "override", "Overrode"
        REGENERATE = "regenerate", "Regenerated"

    plan = models.ForeignKey(PreparationPlan, on_delete=models.CASCADE, related_name="decisions")
    action = models.CharField(max_length=12, choices=Action.choices)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="plan_decisions"
    )
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["plan", "-at"], name="plan_decision_idx")]


class DataImport(models.Model):
    class Kind(models.TextChoices):
        PARTICIPATION = "participation", "Participation"
        MENUS = "menus", "Menus"
        PREPARED = "prepared", "Quantities prepared"
        WASTAGE = "wastage", "Wastage"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    filename = models.CharField(max_length=255)
    rows_created = models.PositiveIntegerField(default=0)
    rows_updated = models.PositiveIntegerField(default=0)
    rows_skipped = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="imports"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["kind", "-created_at"], name="dataimport_kind_idx")]

    def __str__(self) -> str:
        return f"{self.kind} {self.filename} ({self.rows_created} new)"
