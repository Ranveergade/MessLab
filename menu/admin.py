from django.contrib import admin

from .models import DailyMenu, Dish, MealSlot, MenuItem


@admin.register(Dish)
class DishAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "diet_type", "avg_portion_grams", "popularity_baseline")
    list_filter = ("category", "diet_type", "is_active")
    search_fields = ("name",)


@admin.register(MealSlot)
class MealSlotAdmin(admin.ModelAdmin):
    list_display = ("name", "start_time", "end_time", "peak_start", "counters")
    ordering = ("order",)


class MenuItemInline(admin.TabularInline):
    model = MenuItem
    extra = 1
    fields = (
        "dish",
        "planned_quantity_kg",
        "is_special",
        "serving_category",
        "portion_grams",
        "frequency_per_month",
    )


@admin.register(DailyMenu)
class DailyMenuAdmin(admin.ModelAdmin):
    list_display = ("date", "meal", "is_special", "notes")
    list_filter = ("meal", "is_special")
    date_hierarchy = "date"
    inlines = [MenuItemInline]


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = (
        "daily_menu",
        "dish",
        "planned_quantity_kg",
        "serving_category",
        "portion_grams",
        "frequency_per_month",
        "is_special",
    )
    list_filter = ("serving_category", "is_special", "dish__category")
