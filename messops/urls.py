"""Routes for the Mess Committee SOP workspace."""
from django.urls import path

from . import views

app_name = "messops"

urlpatterns = [
    path("sop/", views.sop, name="sop"),
    path("student/complaints/", views.student_complaints, name="student-complaints"),
    path(
        "student/menu-preferences/",
        views.student_preferences,
        name="student-preferences",
    ),
    path("manager/committee/", views.manager_committee, name="manager-committee"),
    path("manager/menu-cycle/", views.manager_menu_cycle, name="manager-menu-cycle"),
    path("manager/complaints/", views.manager_complaints, name="manager-complaints"),
    path(
        "manager/complaints/<int:pk>/",
        views.manager_complaint_update,
        name="manager-complaint-update",
    ),
    path("menu/board/", views.menu_board, name="menu-board"),
]
