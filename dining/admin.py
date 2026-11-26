from django.contrib import admin

from .models import (
    CheckIn,
    CheckInDish,
    DataImport,
    Forecast,
    MealIntent,
    MealService,
    PlanDecisionLog,
    PreparationPlan,
    WasteLog,
)


class CheckInDishInline(admin.TabularInline):
    model = CheckInDish
    extra = 0


@admin.register(MealIntent)
class MealIntentAdmin(admin.ModelAdmin):
    list_display = ("student", "date", "meal", "status", "source", "expected_arrival_window")
    list_filter = ("status", "source", "meal", "date")
    search_fields = ("student__username", "student__profile__student_code")
    date_hierarchy = "date"


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ("student", "date", "meal", "timestamp", "clean_plate", "source")
    list_filter = ("meal", "clean_plate", "source", "date")
    inlines = [CheckInDishInline]
    date_hierarchy = "date"


@admin.register(MealService)
class MealServiceAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "meal",
        "actual_participants",
        "declared_intents",
        "weather",
        "is_exam",
        "is_holiday",
        "is_special_menu",
    )
    list_filter = ("meal", "weather", "is_exam", "is_holiday", "is_special_menu")
    date_hierarchy = "date"


@admin.register(WasteLog)
class WasteLogAdmin(admin.ModelAdmin):
    list_display = ("date", "meal", "dish", "prepared_kg", "consumed_kg", "wasted_kg")
    list_filter = ("meal", "dish", "leftover_reusable")
    date_hierarchy = "date"


@admin.register(Forecast)
class ForecastAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "meal",
        "dish",
        "predicted_participation",
        "predicted_demand_kg",
        "model_version",
    )
    list_filter = ("meal", "model_version")
    date_hierarchy = "date"


@admin.register(PreparationPlan)
class PreparationPlanAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "meal",
        "dish",
        "recommended_kg",
        "baseline_kg",
        "status",
        "approved_by",
    )
    list_filter = ("status", "meal", "date")
    date_hierarchy = "date"


@admin.register(PlanDecisionLog)
class PlanDecisionLogAdmin(admin.ModelAdmin):
    list_display = ("plan", "action", "actor", "at")
    list_filter = ("action",)


@admin.register(DataImport)
class DataImportAdmin(admin.ModelAdmin):
    list_display = ("kind", "filename", "rows_created", "rows_updated", "created_at")
    list_filter = ("kind",)


