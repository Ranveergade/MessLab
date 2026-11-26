from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import LoginEvent, OTPCode, Profile, StaffFeedback, StudentProfile, User


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    extra = 0
    fk_name = "user"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    search_fields = ("username", "email", "first_name", "last_name", "profile__phone")
    inlines = [ProfileInline]
    fieldsets = BaseUserAdmin.fieldsets + (("MessLab", {"fields": ("role",)}),)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "phone", "hostel")
    list_filter = ("role", "hostel")
    search_fields = ("user__username", "user__email", "phone")
    autocomplete_fields = ("user",)


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("student_code", "user", "hostel", "diet")
    list_filter = ("hostel", "diet")
    search_fields = ("student_code", "user__username")
    autocomplete_fields = ("user",)


@admin.register(StaffFeedback)
class StaffFeedbackAdmin(admin.ModelAdmin):
    list_display = ("author", "kind", "status", "short_message", "created_at")
    list_filter = ("kind", "status", "author__role", "created_at")
    search_fields = ("author__username", "message", "supervisor_note")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message[:80]


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "expires_at", "attempts", "consumed_at")
    list_filter = ("created_at", "expires_at")
    search_fields = ("user__username", "user__profile__phone")
    readonly_fields = (
        "user",
        "code_hash",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "at")
    list_filter = ("role",)
    search_fields = ("user__username",)
