from django.contrib import admin

from .models import Badge, RewardEvent


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "points", "threshold", "icon")


@admin.register(RewardEvent)
class RewardEventAdmin(admin.ModelAdmin):
    list_display = ("student", "date", "reason", "points", "detail")
    list_filter = ("reason", "date")
    search_fields = ("student__username", "unique_key")
    date_hierarchy = "date"
