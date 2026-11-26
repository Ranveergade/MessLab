"""Versioned DRF API routes."""
from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import api_views as views

app_name = "api"

urlpatterns = [
    path("health/", views.health, name="health"),
    path("auth/otp/request/", views.auth_otp_request, name="auth-otp-request"),
    path("auth/otp/verify/", views.auth_otp_verify, name="auth-otp-verify"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout/", views.auth_logout, name="auth-logout"),
    path("auth/me/", views.auth_me, name="auth-me"),
    path("auth/register/", views.auth_register, name="auth-register"),
    path(
        "auth/register/verify/",
        views.auth_register_verify,
        name="auth-register-verify",
    ),
    # Student
    path("my-plan/", views.my_plan, name="my-plan"),
    path("intents/", views.intents, name="intents"),
    path("meals/today/", views.meals_today, name="meals-today"),
    path("privacy/export/", views.privacy_export, name="privacy-export"),
    path("privacy/delete/", views.privacy_delete, name="privacy-delete"),
    path("meal-feedback/", views.meal_feedback, name="meal-feedback"),
    path("check-in/", views.check_in, name="check-in"),
    path("checkin/pass/", views.checkin_pass, name="checkin-pass"),
    path("staff/scan/", views.staff_scan, name="staff-scan"),
    path(
        "staff/check-in/manual/",
        views.staff_manual_checkin,
        name="staff-manual-checkin",
    ),
    path("crowd/", views.crowd, name="crowd"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("rewards/me/", views.rewards_me, name="rewards-me"),
    # Manager
    path("manager/preparation-plan/", views.manager_preparation_plan, name="manager-plan"),
    path(
        "manager/preparation-plan/<int:pk>/approve/",
        views.approve_plan,
        name="manager-plan-approve",
    ),
    path(
        "manager/preparation-plan/<int:pk>/override/",
        views.override_plan,
        name="manager-plan-override",
    ),
    path("manager/live/", views.manager_live, name="manager-live"),
    path("manager/waste/", views.manager_waste, name="manager-waste"),
    path("manager/waste/record/", views.manager_waste_record, name="manager-waste-record"),
    path("manager/waste/logs/", views.manager_waste_logs, name="manager-waste-logs"),
    path("manager/close-day/", views.manager_close_day, name="manager-close-day"),
    path("manager/impact/", views.manager_impact, name="manager-impact"),
    path("manager/menu-insights/", views.manager_menu_insights, name="manager-menu-insights"),
    path("manager/peak/", views.manager_peak, name="manager-peak"),
    path("manager/model-health/", views.manager_model_health, name="manager-model-health"),
    path("manager/retrain/", views.manager_retrain, name="manager-retrain"),
    path("manager/import-csv/", views.manager_import_csv, name="manager-import-csv"),
    path("manager/export/plan.csv", views.manager_export_plan, name="manager-export-plan"),
    # Menu management
    path("manager/dishes/", views.manager_dishes, name="manager-dishes"),
    path("manager/dishes/<int:pk>/", views.manager_dish_detail, name="manager-dish-detail"),
    path("manager/menus/", views.manager_menus, name="manager-menus"),
    path("manager/menus/<int:pk>/", views.manager_menu_detail, name="manager-menu-detail"),
    # Account administration
    path("manager/users/", views.manager_users, name="manager-users"),
    path("manager/users/<int:pk>/", views.manager_user_detail, name="manager-user-detail"),
    path(
        "manager/users/<int:pk>/role/",
        views.manager_user_role,
        name="manager-user-role",
    ),
    path(
        "manager/users/<int:pk>/active/",
        views.manager_user_active,
        name="manager-user-active",
    ),
    path("manager/audit/", views.manager_audit, name="manager-audit"),
]
