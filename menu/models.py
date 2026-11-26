"""Menu catalogue: dishes, meal slots, daily menus and per-dish planned quantities."""
from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Dish(models.Model):
    class Category(models.TextChoices):
        MAIN = "main", "Main course"
        RICE = "rice", "Rice / grains"
        BREAD = "bread", "Bread"
        SIDE = "side", "Side / dal"
        DESSERT = "dessert", "Dessert"
        BEVERAGE = "beverage", "Beverage"

    class DietType(models.TextChoices):
        VEG = "veg", "Vegetarian"
        NONVEG = "non-veg", "Non-vegetarian"
        JAIN = "jain", "Jain"
        EGG = "egg", "Contains egg"

    name = models.CharField(max_length=120, unique=True)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.MAIN)
    diet_type = models.CharField(max_length=12, choices=DietType.choices, default=DietType.VEG)
    avg_portion_grams = models.PositiveIntegerField(
        default=180, validators=[MinValueValidator(20), MaxValueValidator(900)]
    )
    popularity_baseline = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Relative baseline appeal, 0-1. Seeded from historical uptake.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["category"], name="dish_category_idx"),
            models.Index(fields=["is_active"], name="dish_active_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(popularity_baseline__gte=0)
                & models.Q(popularity_baseline__lte=1),
                name="dish_popularity_0_1",
            ),
            models.CheckConstraint(
                condition=models.Q(avg_portion_grams__gt=0), name="dish_portion_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def diet_icon(self) -> str:
        return {"veg": "🟢", "non-veg": "🔴", "jain": "🟡", "egg": "🟠"}.get(
            self.diet_type, "⚪"
        )


class MealSlot(models.Model):
    class Name(models.TextChoices):
        BREAKFAST = "breakfast", "Breakfast"
        LUNCH = "lunch", "Lunch"
        SNACKS = "snacks", "Snacks"
        DINNER = "dinner", "Dinner"

    name = models.CharField(
        max_length=32,
        unique=True,
        help_text="Machine name, e.g. breakfast. Managers can add new slots.",
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    peak_start = models.TimeField(help_text="Start of the historically busiest 30 minutes.")
    peak_end = models.TimeField()
    counters = models.PositiveSmallIntegerField(default=3, help_text="Number of serving counters.")
    counter_capacity_per_hour = models.PositiveIntegerField(
        default=420, help_text="Students one counter can serve per hour."
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "start_time"]
        indexes = [models.Index(fields=["order"], name="mealslot_order_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(counters__gte=1), name="mealslot_counters_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(counter_capacity_per_hour__gt=0),
                name="mealslot_capacity_positive",
            ),
        ]

    _NAME_LABELS = dict(Name.choices)

    def get_name_display(self) -> str:
        return self._NAME_LABELS.get(self.name, self.name.replace("_", " ").title())

    def __str__(self) -> str:
        return self.get_name_display()

    @property
    def hourly_capacity(self) -> int:
        return self.counters * self.counter_capacity_per_hour

    @property
    def duration_minutes(self) -> int:
        start = self.start_time.hour * 60 + self.start_time.minute
        end = self.end_time.hour * 60 + self.end_time.minute
        return max(1, end - start)

    @property
    def peak_label(self) -> str:
        return f"{self.peak_start.strftime('%H:%M')}–{self.peak_end.strftime('%H:%M')}"


class DailyMenu(models.Model):
    date = models.DateField(db_index=True)
    meal = models.ForeignKey(MealSlot, on_delete=models.PROTECT, related_name="daily_menus")
    notes = models.CharField(max_length=240, blank=True)
    is_special = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "meal__order"]
        constraints = [
            models.UniqueConstraint(fields=["date", "meal"], name="uniq_daily_menu_date_meal")
        ]
        indexes = [models.Index(fields=["date", "meal"], name="dailymenu_date_meal_idx")]

    def __str__(self) -> str:
        return f"{self.date} {self.meal.name}"

    @property
    def total_planned_kg(self) -> float:
        return sum(float(item.planned_quantity_kg) for item in self.items.all())


class MenuItem(models.Model):
    class ServingCategory(models.TextChoices):
        UNLIMITED = "unlimited", "Unlimited"
        SPECIAL = "special", "Special"
        LIMITED = "limited", "Limited quantity"

    daily_menu = models.ForeignKey(DailyMenu, on_delete=models.CASCADE, related_name="items")
    dish = models.ForeignKey(Dish, on_delete=models.PROTECT, related_name="menu_items")
    planned_quantity_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_special = models.BooleanField(default=False)
    serving_category = models.CharField(
        max_length=12,
        choices=ServingCategory.choices,
        default=ServingCategory.UNLIMITED,
        help_text="SOP §5: unlimited, special or limited-quantity item.",
    )
    portion_grams = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(2000)],
        help_text="Fixed portion per student for limited-quantity items.",
    )
    frequency_per_month = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="How many times this special or limited item is served in the month.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["dish__category", "dish__name"]
        constraints = [
            models.UniqueConstraint(fields=["daily_menu", "dish"], name="uniq_menu_item_dish"),
            models.CheckConstraint(
                condition=models.Q(planned_quantity_kg__gte=0),
                name="menu_item_planned_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(portion_grams__isnull=True)
                | models.Q(portion_grams__gte=1),
                name="menu_item_portion_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(frequency_per_month__isnull=True)
                | models.Q(frequency_per_month__gte=1),
                name="menu_item_frequency_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["daily_menu", "dish"], name="menuitem_menu_dish_idx")
        ]

    def __str__(self) -> str:
        return f"{self.daily_menu} — {self.dish.name}"

    @property
    def is_veg_friendly(self) -> bool:
        return self.dish.diet_type in {Dish.DietType.VEG, Dish.DietType.JAIN}

    @property
    def serving_note(self) -> str:
        """Human-readable SOP §5 classification shown on student and board pages."""
        parts: list[str] = []
        if self.serving_category in {self.ServingCategory.LIMITED}:
            if self.portion_grams:
                parts.append(f"{self.portion_grams} g portion")
            else:
                parts.append("fixed portion")
        if self.serving_category in {
            self.ServingCategory.SPECIAL,
            self.ServingCategory.LIMITED,
        } and self.frequency_per_month:
            parts.append(f"{self.frequency_per_month}×/month")
        if not parts and self.serving_category in {self.ServingCategory.UNLIMITED}:
            return "Unlimited"
        return " · ".join(parts) or self.get_serving_category_display()
