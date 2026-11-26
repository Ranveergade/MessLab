"""Phase 4: role landing pages."""
from __future__ import annotations

from django.test import TestCase

from accounts.models import User
from dining.models import CheckIn, PreparationPlan
from tests.factories import (
    BASE_DATE,
    make_dish,
    make_plan,
    make_slots,
    make_student,
    make_user,
    set_today,
)

HINDI = "आज की तैयारी"  # "Today's preparation"


class RolePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.dish = make_dish("Rajma Masala")
        cls.second = make_dish("Jeera Rice", category="rice")
        make_plan(
            BASE_DATE,
            cls.slots["lunch"],
            cls.dish,
            recommended=12,
            baseline=18,
            status=PreparationPlan.Status.APPROVED,
        )
        make_plan(
            BASE_DATE,
            cls.slots["lunch"],
            cls.second,
            recommended=8,
            baseline=12,
            status=PreparationPlan.Status.APPROVED,
        )
        cls.worker = make_user("role-worker", User.Role.WORKER, email="worker@messlab.test")
        cls.canteen = make_user(
            "role-canteen", User.Role.CANTEEN_STAFF, email="canteen@messlab.test"
        )
        cls.supervisor = make_user(
            "role-supervisor", User.Role.SUPERVISOR, email="sup@messlab.test"
        )
        cls.student = make_student("role-student", hostel="H1")
        cls.student.first_name = "Meera"
        cls.student.save(update_fields=["first_name"])
        CheckIn.objects.create(
            student=cls.student, date=BASE_DATE, meal=cls.slots["lunch"]
        )

    def test_worker_page_shows_the_prep_sheet_without_a_scanner_link(self):
        self.client.force_login(self.worker)
        response = self.client.get("/staff/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rajma Masala")
        self.assertContains(response, "20.0 kg")
        self.assertContains(response, "Approved today")
        self.assertNotContains(response, "/staff/scan/")

    def test_canteen_page_shows_counts_and_pseudonymous_codes(self):
        self.client.force_login(self.canteen)
        response = self.client.get("/staff/scan/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.student.profile.student_code)
        self.assertContains(response, "Checked in today")
        self.assertNotContains(response, "Meera")

    def test_canteen_worker_gets_a_scanner_shortcut_on_the_prep_sheet(self):
        self.client.force_login(self.canteen)
        response = self.client.get("/staff/")
        self.assertContains(response, "/staff/scan/")

    def test_supervisor_page_compares_plan_and_checkins(self):
        self.client.force_login(self.supervisor)
        response = self.client.get("/supervisor/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preparation today")
        self.assertContains(response, "20.0 kg")
        self.assertContains(response, "Open feedback")
        self.assertEqual(
            [row["total_kg"] for row in response.context["prep"]], [20.0]
        )
