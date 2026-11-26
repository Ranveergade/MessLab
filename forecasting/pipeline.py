"""Train, persist and apply the two-stage forecast."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import StudentProfile
from dining.models import (
    DayContext,
    Forecast,
    MealIntent,
    MealService,
    PreparationPlan,
)
from dining.services import (
    average_kg_per_participant,
    declared_positive_intents,
    round_kg,
    today,
)
from menu.models import DailyMenu, MealSlot

from .features import (
    DISH_FEATURES,
    PARTICIPATION_FEATURES,
    dish_feature_rows,
    dish_training_frame,
    participation_feature_row,
    participation_training_frame,
)
from .model import (
    InsufficientData,
    fit_dish,
    fit_participation,
    predict_participation,
    predict_shares,
    safety_buffer,
)
from .models import ModelRun

MEAL_BASE_RATE = {"breakfast": 0.62, "lunch": 0.88, "snacks": 0.48, "dinner": 0.84}


def store_dir() -> Path:
    path = Path(settings.MESSLAB["ML_STORE_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path() -> Path:
    return store_dir() / "models.joblib"


def metadata_path() -> Path:
    return store_dir() / "models_meta.json"


def _new_version() -> str:
    return f"v{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


def load_artifact() -> dict | None:
    path = artifact_path()
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception:
        return None


def _fallback_participation(date: dt.date, slot: MealSlot, declared: int) -> tuple[float, float, float]:
    history = list(
        MealService.objects.filter(meal=slot, date__lt=date)
        .order_by("-date")
        .values_list("actual_participants", flat=True)[:28]
    )
    if history:
        same_weekday = list(
            MealService.objects.filter(
                meal=slot, date__lt=date, date__week_day=((date.weekday() + 1) % 7) + 1
            )
            .order_by("-date")
            .values_list("actual_participants", flat=True)[:4]
        )
        base = float(np.mean(same_weekday or history[:7]))
    else:
        population = StudentProfile.objects.count() or 1500
        base = population * MEAL_BASE_RATE.get(slot.name, 0.7)
        if slot.name == "breakfast" and date.weekday() == 6:
            base *= 0.45
    if declared > 0:
        implied = declared / max(0.05, settings.MESSLAB["DEFAULT_INTENT_COVERAGE"])
        pred = 0.55 * base + 0.45 * implied
    else:
        pred = base
    ctx = DayContext.objects.filter(date=date).first()
    if ctx:
        if ctx.is_holiday:
            pred *= 0.5
        if ctx.is_exam:
            pred *= 1.04
        if ctx.weather == "rain":
            pred *= 0.94
    pred = max(0.0, pred)
    spread = max(pred * 0.12, 12.0)
    return pred, max(0.0, pred - spread), pred + spread


def _fallback_shares(menu: DailyMenu) -> list[tuple[object, float]]:
    items = list(menu.items.all())
    weights = []
    for item in items:
        weight = max(0.01, item.dish.popularity_baseline)
        if item.is_special:
            weight *= 1.3
        weights.append(weight)
    total = sum(weights) or 1.0
    return [(item, weight / total) for item, weight in zip(items, weights)]


def _meal_context_defaults(date: dt.date) -> DayContext:
    ctx, _ = DayContext.objects.get_or_create(date=date)
    return ctx


@transaction.atomic
def generate_forecasts(
    start_date: dt.date | None = None,
    days: int = 8,
    artifact: dict | None = None,
    force: bool = False,
) -> dict:
    """Create/refresh meal and dish forecasts plus draft preparation plans."""
    start_date = start_date or today()
    artifact = artifact if artifact is not None else load_artifact()
    avg_kg = (
        float(artifact["dish"]["metrics"].get("avg_kg_per_participant", 0.0))
        if artifact
        else 0.0
    )
    if avg_kg <= 0:
        avg_kg = average_kg_per_participant()
    avg_kg_by_meal = (
        artifact["dish"]["metrics"].get("avg_kg_by_meal", {}) if artifact else {}
    )
    sl = settings.MESSLAB["SERVICE_LEVEL"]
    min_pct = settings.MESSLAB["MIN_BUFFER_PCT"]
    max_pct = settings.MESSLAB["MAX_BUFFER_PCT"]
    version = artifact["version"] if artifact else "fallback-heuristic"
    created = 0
    meals_covered = 0

    for offset in range(days):
        date = start_date + dt.timedelta(days=offset)
        _meal_context_defaults(date)
        for slot in MealSlot.objects.all():
            menu = (
                DailyMenu.objects.filter(date=date, meal=slot)
                .prefetch_related("items__dish")
                .first()
            )
            if not menu:
                continue
            meals_covered += 1
            declared = declared_positive_intents(date, slot)
            if artifact:
                X = participation_feature_row(date, slot, declared)
                pred, lower, upper = predict_participation(artifact["participation"], X)
                pred_f, low_f, up_f = float(pred[0]), float(lower[0]), float(upper[0])
            else:
                pred_f, low_f, up_f = _fallback_participation(date, slot, declared)
            meal_avg_kg = float(avg_kg_by_meal.get(slot.name, avg_kg))
            pred_int = int(round(max(0.0, pred_f)))
            low_int = int(round(max(0.0, low_f)))
            up_int = int(round(max(up_f, pred_f)))
            Forecast.objects.update_or_create(
                date=date,
                meal=slot,
                dish=None,
                defaults={
                    "predicted_participation": pred_int,
                    "predicted_demand_kg": round_kg(pred_f * meal_avg_kg),
                    "lower_bound_kg": round_kg(low_f * meal_avg_kg),
                    "upper_bound_kg": round_kg(max(up_f, pred_f) * meal_avg_kg),
                    "lower_participation": low_int,
                    "upper_participation": up_int,
                    "model_version": version,
                },
            )

            if artifact:
                Xd = dish_feature_rows(date, slot, menu, pred_int)
                shares = predict_shares(artifact["dish"], Xd)
                total = float(np.sum(shares)) or 1.0
                shares = shares / total
                dish_metrics = artifact["dish"]["metrics"]
                q05 = float(dish_metrics.get("q05_kg", -0.15))
                q95 = float(dish_metrics.get("q95_kg", 0.15))
                dish_rows = list(zip(list(menu.items.all()), shares))
            else:
                dish_rows = _fallback_shares(menu)
                q05, q95 = -0.15, 0.15

            for item, share in dish_rows:
                demand = max(0.0, pred_f * meal_avg_kg * float(share))
                lower = max(0.0, demand + q05)
                upper = max(demand, demand + q95)
                recommended = safety_buffer(demand, lower, upper, sl, min_pct, max_pct)
                baseline = float(item.planned_quantity_kg) if item.planned_quantity_kg else demand * 1.2
                if baseline <= 0:
                    baseline = demand * 1.2
                Forecast.objects.update_or_create(
                    date=date,
                    meal=slot,
                    dish=item.dish,
                    defaults={
                        "predicted_participation": pred_int,
                        "predicted_demand_kg": round_kg(demand),
                        "lower_bound_kg": round_kg(lower),
                        "upper_bound_kg": round_kg(upper),
                        "lower_participation": low_int,
                        "upper_participation": up_int,
                        "model_version": version,
                    },
                )
                existing = PreparationPlan.objects.filter(
                    date=date, meal=slot, dish=item.dish
                ).first()
                if existing and existing.status != PreparationPlan.Status.DRAFT and not force:
                    continue
                PreparationPlan.objects.update_or_create(
                    date=date,
                    meal=slot,
                    dish=item.dish,
                    defaults={
                        "recommended_kg": round_kg(recommended),
                        "baseline_kg": round_kg(baseline),
                        "status": PreparationPlan.Status.DRAFT,
                    },
                )
                created += 1
    return {
        "dates": days,
        "meals": meals_covered,
        "plans": created,
        "model_version": version,
    }


@transaction.atomic
def train_and_store(validation_days: int = 21, generate: bool = True, days: int = 8) -> ModelRun:
    part_df, part_features = participation_training_frame()
    dish_df, dish_features = dish_training_frame()
    if part_df.empty:
        raise InsufficientData(
            "No MealService rows found. Run `python manage.py seed_demo` or import CSVs first."
        )
    if dish_df.empty:
        raise InsufficientData(
            "No WasteLog rows found. Import wastage data or run `python manage.py seed_demo`."
        )

    part_fit = fit_participation(part_df, part_features, validation_days)
    dish_fit = fit_dish(dish_df, dish_features, validation_days)
    version = _new_version()

    artifact = {
        "version": version,
        "trained_at": timezone.now().isoformat(),
        "participation": {
            "model": part_fit.model,
            "features": part_features,
            "medians": part_fit.medians,
            "metrics": part_fit.metrics,
            "algorithm": part_fit.algorithm,
        },
        "dish": {
            "model": dish_fit.model,
            "features": dish_features,
            "medians": dish_fit.medians,
            "metrics": dish_fit.metrics,
            "algorithm": dish_fit.algorithm,
        },
    }
    joblib.dump(artifact, artifact_path())
    metadata_path().write_text(
        json.dumps(
            {
                "version": version,
                "trained_at": artifact["trained_at"],
                "participation": part_fit.metrics,
                "dish": dish_fit.metrics,
                "participation_algorithm": part_fit.algorithm,
                "dish_algorithm": dish_fit.algorithm,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    ModelRun.objects.filter(is_active=True).update(is_active=False)
    run = ModelRun.objects.create(
        version=version,
        train_rows=part_fit.metrics["train_rows"],
        validation_rows=part_fit.metrics["validation_rows"],
        train_start=dt.date.fromisoformat(part_fit.metrics["train_start"]),
        train_end=dt.date.fromisoformat(part_fit.metrics["train_end"]),
        validation_start=dt.date.fromisoformat(part_fit.metrics["validation_start"]),
        validation_end=dt.date.fromisoformat(part_fit.metrics["validation_end"]),
        participation_mae=part_fit.metrics["mae"],
        participation_mape=part_fit.metrics["mape"],
        participation_baseline_mae=part_fit.metrics["baseline_mae"],
        participation_baseline_mape=part_fit.metrics["baseline_mape"],
        dish_mae_kg=dish_fit.metrics["mae_kg"],
        dish_mape=dish_fit.metrics["mape"],
        dish_baseline_mae_kg=dish_fit.metrics["baseline_mae_kg"],
        dish_baseline_mape=dish_fit.metrics["baseline_mape"],
        participation_algorithm=part_fit.algorithm,
        dish_algorithm=dish_fit.algorithm,
        metrics={
            "participation": part_fit.metrics,
            "dish": dish_fit.metrics,
            "service_level": settings.MESSLAB["SERVICE_LEVEL"],
            "buffer_min_pct": settings.MESSLAB["MIN_BUFFER_PCT"],
            "buffer_max_pct": settings.MESSLAB["MAX_BUFFER_PCT"],
        },
        is_active=True,
    )
    if generate:
        generate_forecasts(days=days, artifact=artifact, force=True)
    return run


def retrain_and_regenerate(validation_days: int = 21, days: int = 8) -> ModelRun:
    return train_and_store(validation_days=validation_days, generate=True, days=days)
