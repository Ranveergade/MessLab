from django.contrib import admin

from .models import ModelRun


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    list_display = (
        "version",
        "trained_at",
        "participation_mae",
        "participation_baseline_mae",
        "dish_mae_kg",
        "is_active",
    )
    list_filter = ("is_active", "participation_algorithm")
