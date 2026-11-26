from django.contrib import admin

from .models import (
    CommitteeMember,
    Complaint,
    MenuChangeRequest,
    MenuPreference,
    MonthlyMenuCycle,
    ReviewMeeting,
)


@admin.register(CommitteeMember)
class CommitteeMemberAdmin(admin.ModelAdmin):
    list_display = ("display_name", "role", "program", "term_start", "is_active")
    list_filter = ("role", "program", "is_active")
    search_fields = ("full_name", "email", "user__username")


@admin.register(MonthlyMenuCycle)
class MonthlyMenuCycleAdmin(admin.ModelAdmin):
    list_display = ("month", "status", "approved_by_name", "approved_at", "published_at")
    list_filter = ("status",)
    date_hierarchy = "month"


@admin.register(MenuPreference)
class MenuPreferenceAdmin(admin.ModelAdmin):
    list_display = ("cycle", "student", "meal", "updated_at")
    list_filter = ("cycle", "meal")
    search_fields = ("student__username", "student__profile__student_code")


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ("reference", "category", "status", "service_date", "created_at")
    list_filter = ("status", "category", "meal")
    search_fields = ("description", "student__username", "student__profile__student_code")
    date_hierarchy = "created_at"


@admin.register(MenuChangeRequest)
class MenuChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("service_date", "meal", "summary", "status", "agreed_at")
    list_filter = ("status", "meal")
    date_hierarchy = "service_date"


@admin.register(ReviewMeeting)
class ReviewMeetingAdmin(admin.ModelAdmin):
    list_display = ("meeting_date", "month", "escalated", "created_by")
    list_filter = ("escalated",)
    date_hierarchy = "meeting_date"
