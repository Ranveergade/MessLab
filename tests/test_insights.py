"""Preparation-plan, impact and analytics arithmetic."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from dining.models import PreparationPlan
from insights.api_views import _plan_rows
from insights.services import (
    crowd_forecast,
    impact_summary,
    live_meal_status,
    peak_analysis,
    waste_analytics,
)
from tests.factories import (
    BASE_DATE,
    make_checkin,
    make_dish,
    make_forecast,
    make_menu,
    make_plan,
    make_service,
    make_slots,
    make_student,
    make_waste,
    set_today,
)


class PrepPlanMathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish_a = make_dish("Aloo Gobi")
        cls.dish_b = make_dish("Curd Rice", category="rice")
        set_today(BASE_DATE)
        make_menu(BASE_DATE, cls.slots["lunch"], [(cls.dish_a, 15), (cls.dish_b, 8)])
        make_forecast(BASE_DATE, cls.slots["lunch"], participation=150, demand=23)
        make_forecast(
            BASE_DATE, cls.slots["lunch"], dish=cls.dish_a, participation=150, demand=15
        )
        make_forecast(
            BASE_DATE, cls.slots["lunch"], dish=cls.dish_b, participation=150, demand=8
        )
        make_plan(BASE_DATE, cls.slots["lunch"], cls.dish_a, 10, 15)
        make_plan(BASE_DATE, cls.slots["lunch"], cls.dish_b, 8, 8)

    def test_plan_totals_and_saved_kg(self):
        rows, totals = _plan_rows(BASE_DATE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(totals["dishes"], 2)
        self.assertAlmostEqual(totals["recommended_kg"], 18.0)
        self.assertAlmostEqual(totals["baseline_kg"], 23.0)
        self.assertAlmostEqual(totals["saved_kg"], 5.0)
        by_dish = {row["dish"]: row for row in rows}
        self.assertAlmostEqual(by_dish["Aloo Gobi"]["projected_waste_avoided_kg"], 5.0)
        self.assertAlmostEqual(by_dish["Curd Rice"]["projected_waste_avoided_kg"], 0.0)

    def test_plan_rows_carry_forecast_and_intent_context(self):
        rows, _totals = _plan_rows(BASE_DATE)
        row = next(r for r in rows if r["dish"] == "Aloo Gobi")
        self.assertEqual(row["meal"], "lunch")
        self.assertEqual(row["forecast"]["predicted_demand_kg"], 15.0)
        self.assertEqual(row["forecast"]["predicted_participation"], 150)
        self.assertIn("POSITIVE", row["intents"])


class ImpactMathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish_a = make_dish("Chole")
        cls.dish_b = make_dish("Jeera Rice", category="rice")
        cls.plan_date = dt.date(2026, 3, 5)
        set_today(dt.date(2026, 3, 10))
        make_service(cls.plan_date, cls.slots["lunch"], 100)
        make_plan(cls.plan_date, cls.slots["lunch"], cls.dish_a, 15, 20)
        make_plan(cls.plan_date, cls.slots["lunch"], cls.dish_b, 9, 10)
        make_waste(cls.plan_date, cls.slots["lunch"], cls.dish_a, 20, 15.5, 4.5)
        make_waste(cls.plan_date, cls.slots["lunch"], cls.dish_b, 10, 9.9, 0.1)

    def test_impact_summary_subtracts_recommended_from_baseline_waste(self):
        impact = impact_summary(days=30)
        self.assertFalse(impact["empty"])
        self.assertEqual(impact["matched_rows"], 2)
        self.assertEqual(impact["participants"], 100)
        self.assertAlmostEqual(impact["without"]["waste_kg"], 4.6)
        self.assertAlmostEqual(impact["with"]["waste_kg"], 0.0)
        self.assertAlmostEqual(impact["avoided"]["waste_kg"], 4.6)
        self.assertEqual(impact["avoided"]["cost_inr"], round(4.6 * 185))
        self.assertAlmostEqual(impact["avoided"]["co2e_kg"], round(4.6 * 2.5, 1))
        self.assertAlmostEqual(impact["avoided"]["prepared_kg"], 6.0)

    def test_waste_analytics_totals(self):
        analytics = waste_analytics(days=30)
        self.assertFalse(analytics["empty"])
        self.assertAlmostEqual(analytics["totals"]["prepared_kg"], 30.0)
        self.assertAlmostEqual(analytics["totals"]["consumed_kg"], 25.4)
        self.assertAlmostEqual(analytics["totals"]["wasted_kg"], 4.6)
        self.assertAlmostEqual(analytics["totals"]["waste_pct"], 15.3)
        self.assertEqual(analytics["totals"]["cost_inr"], round(4.6 * 185))
        self.assertEqual(analytics["totals"]["shortage_incidents"], 1)


class AnalyticsShapeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Pav Bhaji")
        cls.student = make_student("analytics-student")
        cls.date = BASE_DATE
        set_today(cls.date)
        make_menu(cls.date, cls.slots["lunch"], [(cls.dish, 20)])
        make_forecast(cls.date, cls.slots["lunch"], participation=100, demand=20)
        make_forecast(
            cls.date, cls.slots["lunch"], dish=cls.dish, participation=100, demand=20
        )
        make_plan(cls.date, cls.slots["lunch"], cls.dish, 18, 20)

    def test_crowd_forecast_shape(self):
        crowd = crowd_forecast(self.date, self.slots["lunch"])
        self.assertEqual(crowd["meal"], self.slots["lunch"])
        self.assertGreater(len(crowd["buckets"]), 0)
        self.assertIn(crowd["overall_level"], {"low", "medium", "high"})
        self.assertGreaterEqual(crowd["peak_risk"], 0)

    def test_peak_analysis_heatmap_shape(self):
        previous = self.date - dt.timedelta(days=1)
        timestamp = timezone.make_aware(
            dt.datetime.combine(previous, dt.time(13, 10)),
            timezone.get_current_timezone(),
        )
        make_checkin(self.student, previous, self.slots["lunch"])
        from dining.models import CheckIn

        CheckIn.objects.filter(student=self.student, date=previous).update(timestamp=timestamp)
        peak = peak_analysis(self.date, self.slots["lunch"])
        self.assertEqual(peak["meal"], self.slots["lunch"])
        self.assertEqual(len(peak["heatmap"]), 7)
        self.assertTrue(all(len(day["cells"]) == len(peak["buckets"]) for day in peak["heatmap"]))

    def test_live_meal_status_shape(self):
        live = live_meal_status(self.date, self.slots["lunch"])
        self.assertEqual(live["meal"], self.slots["lunch"])
        self.assertIn("dishes", live)
        self.assertIn("alerts", live)
        self.assertGreaterEqual(live["remaining_expected"], 0)
        for dish in live["dishes"]:
            self.assertGreaterEqual(dish["prepared_kg"], 0)
            self.assertGreaterEqual(dish["remaining_kg"], 0)
