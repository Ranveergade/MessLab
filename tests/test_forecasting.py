"""Forecast feature leakage, time splits and output contracts."""
from __future__ import annotations

import datetime as dt
import tempfile
from unittest.mock import patch

import pandas as pd
from django.test import TestCase, override_settings

from dining.models import Forecast, PreparationPlan
from forecasting.features import (
    DISH_FEATURES,
    PARTICIPATION_FEATURES,
    dish_training_frame,
    participation_feature_row,
    participation_training_frame,
)
from forecasting.model import (
    _time_split,
    fit_dish,
    fit_participation,
    safety_buffer,
)
from forecasting.pipeline import generate_forecasts
from messlab import settings as project_settings
from tests.factories import (
    BASE_DATE,
    make_dish,
    make_menu,
    make_service,
    make_slots,
    make_waste,
    set_today,
)


class LeakageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.lunch = cls.slots["lunch"]

    def test_participation_lag_uses_previous_service_only(self):
        make_service(BASE_DATE, self.lunch, 100)
        make_service(BASE_DATE + dt.timedelta(days=1), self.lunch, 200)
        frame, features = participation_training_frame()
        self.assertIn("lag_1", features)
        day2 = frame[frame["date"] == pd.Timestamp(BASE_DATE + dt.timedelta(days=1))].iloc[0]
        self.assertEqual(day2["lag_1"], 100.0)
        self.assertEqual(day2["rolling_mean_7"], 100.0)
        self.assertEqual(day2["actual_participants"], 200)

    def test_prediction_features_ignore_same_day_outcome(self):
        make_service(BASE_DATE, self.lunch, 100)
        make_service(BASE_DATE + dt.timedelta(days=1), self.lunch, 500)
        row = participation_feature_row(BASE_DATE + dt.timedelta(days=1), self.lunch, 0)
        self.assertEqual(row.iloc[0]["lag_1"], 100.0)
        self.assertEqual(row.iloc[0]["rolling_mean_7"], 100.0)

    def test_dish_lag_share_uses_previous_day_only(self):
        dish_a = make_dish("Dish A", popularity=0.7)
        dish_b = make_dish("Dish B", popularity=0.3)
        day1 = BASE_DATE
        day2 = BASE_DATE + dt.timedelta(days=1)
        make_menu(day1, self.lunch, [(dish_a, 20), (dish_b, 10)])
        make_menu(day2, self.lunch, [(dish_a, 20), (dish_b, 10)])
        make_service(day1, self.lunch, 100)
        make_service(day2, self.lunch, 120)
        make_waste(day1, self.lunch, dish_a, 40, 30, 10)
        make_waste(day1, self.lunch, dish_b, 20, 10, 10)
        make_waste(day2, self.lunch, dish_a, 50, 40, 10)
        make_waste(day2, self.lunch, dish_b, 20, 10, 10)
        frame, _features = dish_training_frame()
        row = frame[
            (frame["date"] == pd.Timestamp(day2)) & (frame["dish_id"] == float(dish_a.pk))
        ].iloc[0]
        self.assertAlmostEqual(row["lag_1_share"], 0.75, places=6)
        self.assertEqual(row["share"], 0.8)


class TimeSplitTests(TestCase):
    def test_holdout_is_strictly_in_the_future(self):
        dates = [BASE_DATE + dt.timedelta(days=i) for i in range(60)]
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(dates),
                "actual_participants": [100 + i for i in range(60)],
            }
        )
        train, valid, cutoff = _time_split(frame, validation_days=21)
        self.assertLessEqual(pd.to_datetime(train["date"]).max(), cutoff)
        self.assertGreater(pd.to_datetime(valid["date"]).min(), cutoff)
        self.assertLess(
            pd.to_datetime(train["date"]).max(), pd.to_datetime(valid["date"]).min()
        )


class ModelContractTests(TestCase):
    def _participation_frame(self, days: int = 60) -> pd.DataFrame:
        rows = []
        for i in range(days):
            row = {feature: 0.0 for feature in PARTICIPATION_FEATURES}
            row["date"] = pd.Timestamp(BASE_DATE + dt.timedelta(days=i))
            row["weekday"] = float((BASE_DATE + dt.timedelta(days=i)).weekday())
            row["actual_participants"] = 100 + (i % 7) * 5
            rows.append(row)
        return pd.DataFrame(rows)

    def test_participation_model_metrics_are_forward_and_non_negative(self):
        frame = self._participation_frame()
        result = fit_participation(frame, validation_days=21)
        metrics = result.metrics
        self.assertGreaterEqual(metrics["mae"], 0)
        self.assertGreaterEqual(metrics["mape"], 0)
        self.assertGreater(metrics["train_rows"], 0)
        self.assertGreater(metrics["validation_rows"], 0)
        self.assertLess(metrics["validation_start"], metrics["validation_end"] or "9999")
        self.assertLess(metrics["train_end"], metrics["validation_start"])
        self.assertIn(result.algorithm, {"GradientBoosting", "RandomForest"})

    def test_dish_model_returns_non_negative_metrics(self):
        rows = []
        for i in range(60):
            for dish_id in (1.0, 2.0):
                row = {feature: 0.0 for feature in DISH_FEATURES}
                row.update(
                    {
                        "date": pd.Timestamp(BASE_DATE + dt.timedelta(days=i)),
                        "meal": "lunch",
                        "participants": 100.0,
                        "consumed_kg": 10.0,
                        "share": 0.5,
                        "dish_id": dish_id,
                        "weekday": float((BASE_DATE + dt.timedelta(days=i)).weekday()),
                        "popularity": 0.5,
                        "relative_popularity": 0.5,
                        "avg_portion_grams": 180.0,
                    }
                )
                rows.append(row)
        result = fit_dish(pd.DataFrame(rows), validation_days=21)
        self.assertGreaterEqual(result.metrics["mae_kg"], 0)
        self.assertGreaterEqual(result.metrics["mape"], 0)
        self.assertGreaterEqual(result.metrics["avg_kg_per_participant"], 0)
        self.assertIn("lunch", result.metrics["avg_kg_by_meal"])


class SafetyBufferTests(TestCase):
    def test_buffer_stays_within_guardrails(self):
        result = safety_buffer(100, 90, 120, 0.95, 0.02, 0.22)
        self.assertGreaterEqual(result, 102)
        self.assertLessEqual(result, 122)
        self.assertGreaterEqual(result, 100)

    def test_zero_demand_has_zero_buffer(self):
        self.assertEqual(safety_buffer(0, 0, 0, 0.95, 0.02, 0.22), 0.0)


class ForecastGenerationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Veg Pulao", popularity=0.6)
        cls.lunch = cls.slots["lunch"]
        set_today(BASE_DATE)
        make_menu(BASE_DATE, cls.lunch, [(cls.dish, 25)])

    def test_fallback_generation_has_valid_shape_and_non_negative_quantities(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(
            MESSLAB={**project_settings.MESSLAB, "ML_STORE_DIR": tmp}
        ):
            summary = generate_forecasts(start_date=BASE_DATE, days=1)
            self.assertEqual(summary["meals"], 1)
            self.assertEqual(summary["model_version"], "fallback-heuristic")
            self.assertGreater(summary["plans"], 0)

            meal_forecast = Forecast.objects.get(
                date=BASE_DATE, meal=self.lunch, dish=None
            )
            dish_forecast = Forecast.objects.get(
                date=BASE_DATE, meal=self.lunch, dish=self.dish
            )
            for forecast in (meal_forecast, dish_forecast):
                self.assertGreaterEqual(forecast.predicted_demand_kg, 0)
                self.assertGreaterEqual(forecast.lower_bound_kg, 0)
                self.assertGreaterEqual(forecast.upper_bound_kg, 0)
                self.assertLessEqual(forecast.lower_bound_kg, forecast.upper_bound_kg)
                self.assertLessEqual(
                    forecast.lower_participation, forecast.upper_participation
                )

            plan = PreparationPlan.objects.get(
                date=BASE_DATE, meal=self.lunch, dish=self.dish
            )
            self.assertGreaterEqual(plan.recommended_kg, 0)
            self.assertGreaterEqual(plan.baseline_kg, 0)
            self.assertEqual(plan.status, PreparationPlan.Status.DRAFT)
