"""Model-level invariants that keep the operational data honest."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import StudentProfile
from dining.models import Forecast, MealIntent, PreparationPlan
from tests.factories import (
    BASE_DATE,
    make_checkin,
    make_dish,
    make_forecast,
    make_intent,
    make_plan,
    make_slots,
    make_student,
    make_waste,
)


class ConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Dal Tadka")
        cls.student = make_student()

    def test_one_intent_per_student_meal_day(self):
        make_intent(self.student, BASE_DATE, self.slots["lunch"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_intent(self.student, BASE_DATE, self.slots["lunch"])

    def test_one_checkin_per_student_meal_day(self):
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_checkin(self.student, BASE_DATE, self.slots["lunch"])

    def test_waste_cannot_exceed_prepared(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_waste(BASE_DATE, self.slots["lunch"], self.dish, 10, 8, 5)

    def test_waste_consumed_plus_wasted_cannot_exceed_prepared(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_waste(BASE_DATE, self.slots["lunch"], self.dish, 10, 9, 2)

    def test_forecast_bounds_must_be_ordered(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Forecast.objects.create(
                date=BASE_DATE,
                meal=self.slots["lunch"],
                dish=self.dish,
                predicted_participation=100,
                predicted_demand_kg=Decimal("10"),
                lower_bound_kg=Decimal("20"),
                upper_bound_kg=Decimal("10"),
                lower_participation=90,
                upper_participation=110,
            )

    def test_plan_recommended_must_be_non_negative(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_plan(BASE_DATE, self.slots["lunch"], self.dish, -1, 10)

    def test_intent_status_is_constrained(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            MealIntent.objects.create(
                student=self.student,
                date=BASE_DATE,
                meal=self.slots["lunch"],
                status="BANANA",
            )

    def test_forecast_defaults_and_bounds_are_consistent(self):
        forecast = make_forecast(BASE_DATE, self.slots["lunch"], demand=12)
        self.assertGreaterEqual(forecast.lower_bound_kg, 0)
        self.assertLessEqual(forecast.lower_bound_kg, forecast.upper_bound_kg)
        self.assertGreaterEqual(forecast.predicted_demand_kg, 0)


class StudentProfileTests(TestCase):
    def test_profile_is_created_by_signal(self):
        student = make_student("signal-student")
        self.assertTrue(StudentProfile.objects.filter(user=student).exists())
        self.assertEqual(student.profile.student_code, f"H-{student.pk:05d}")

    def test_invalid_default_plan_is_rejected(self):
        student = make_student("plan-student")
        profile = student.profile
        profile.default_plan = {"9": {"lunch": "eat"}}
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_normalized_plan_fills_missing_values(self):
        student = make_student("plan-student-2")
        profile = student.profile
        profile.default_plan = {"0": {"lunch": "skip"}}
        profile.save()
        normalized = profile.normalized_plan()
        self.assertEqual(normalized["0"]["lunch"], "skip")
        self.assertEqual(normalized["0"]["dinner"], "auto")
        self.assertEqual(profile.plan_for(0, "lunch"), "skip")
        self.assertEqual(profile.plan_for(1, "lunch"), "auto")

    def test_plan_waste_avoided_is_never_negative(self):
        slots = make_slots()
        dish = make_dish("Rice")
        plan = make_plan(BASE_DATE, slots["lunch"], dish, 12, 10)
        self.assertEqual(plan.planned_waste_avoided_kg, 0.0)
        plan.recommended_kg = Decimal("8")
        self.assertEqual(plan.planned_waste_avoided_kg, 2.0)

    def test_preparation_plan_is_approved_only_for_approved_states(self):
        slots = make_slots()
        dish = make_dish("Roti")
        draft = make_plan(BASE_DATE, slots["lunch"], dish, 5, 6)
        approved = make_plan(
            BASE_DATE + dt.timedelta(days=1),
            slots["lunch"],
            dish,
            5,
            6,
            status=PreparationPlan.Status.APPROVED,
        )
        self.assertFalse(draft.is_approved)
        self.assertTrue(approved.is_approved)
