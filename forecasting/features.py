"""Feature engineering for the two-stage forecast.

All lag and rolling features are shifted: a row never sees its own outcome or
anything from the future. The train/validation split in ``model.py`` is strictly
time based.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import numpy as np
import pandas as pd
from django.conf import settings

from dining.models import DayContext, MealService, WasteLog
from menu.models import DailyMenu, Dish, MealSlot

MEALS = ["breakfast", "lunch", "snacks", "dinner"]
CATEGORIES = ["main", "rice", "bread", "side", "dessert", "beverage"]
DIETS = ["veg", "non-veg", "jain", "egg"]

PARTICIPATION_FEATURES = [
    "weekday",
    "is_weekend",
    "week_of_semester",
    "is_holiday",
    "is_exam",
    "is_rain",
    "is_hot",
    "is_cold",
    "has_event",
    "is_special_menu",
    "menu_popularity",
    "dish_count",
    "lag_1",
    "lag_7",
    "lag_14",
    "rolling_mean_7",
    "rolling_mean_14",
    "declared_intents",
    "intent_ratio",
    "has_intents",
] + [f"meal_{m}" for m in MEALS]

DISH_FEATURES = [
    "dish_id",
    "dish_weekday",
    "weekday",
    "is_weekend",
    "is_special",
    "dish_count",
    "avg_portion_grams",
    "popularity",
    "relative_popularity",
    "menu_popularity_sum",
    "weekday_x_popularity",
    "lag_1_share",
    "lag_2_share",
    "lag_3_share",
    "lag_7_share",
    "rolling_7_share",
    "rolling_14_share",
    "share_std_7",
    "meal_popularity_sum",
] + [f"meal_{m}" for m in MEALS] + [f"category_{c}" for c in CATEGORIES] + [
    f"diet_{d}" for d in DIETS
]


def _menu_map() -> dict[tuple[dt.date, int], DailyMenu]:
    menus = DailyMenu.objects.select_related("meal").prefetch_related("items__dish")
    return {(m.date, m.meal_id): m for m in menus}


def _context_map() -> dict[dt.date, DayContext]:
    return {c.date: c for c in DayContext.objects.all()}


def _one_hot(row: dict, value: str, prefix: str, options: list[str]) -> None:
    for option in options:
        row[f"{prefix}_{option}"] = 1.0 if value == option else 0.0


def participation_training_frame() -> tuple[pd.DataFrame, list[str]]:
    menus = _menu_map()
    contexts = _context_map()
    services = list(MealService.objects.select_related("meal").order_by("date", "meal__order"))
    if not services:
        return pd.DataFrame(columns=PARTICIPATION_FEATURES + ["actual_participants", "date"]), PARTICIPATION_FEATURES
    first_date = services[0].date
    rows = []
    for service in services:
        menu = menus.get((service.date, service.meal_id))
        items = list(menu.items.all()) if menu else []
        popularity = (
            float(np.mean([i.dish.popularity_baseline for i in items])) if items else 0.5
        )
        ctx = contexts.get(service.date)
        weather = ctx.weather if ctx else service.weather
        row = {
            "date": service.date,
            "meal": service.meal.name,
            "meal_order": service.meal.order,
            "actual_participants": service.actual_participants,
            "declared_intents": service.declared_intents,
            "weekday": service.date.weekday(),
            "is_weekend": 1.0 if service.date.weekday() >= 5 else 0.0,
            "week_of_semester": (service.date - first_date).days // 7,
            "is_holiday": 1.0 if (ctx.is_holiday if ctx else service.is_holiday) else 0.0,
            "is_exam": 1.0 if (ctx.is_exam if ctx else service.is_exam) else 0.0,
            "is_rain": 1.0 if weather == "rain" else 0.0,
            "is_hot": 1.0 if weather == "hot" else 0.0,
            "is_cold": 1.0 if weather == "cold" else 0.0,
            "has_event": 1.0 if (ctx and ctx.event_name) or service.event_name else 0.0,
            "is_special_menu": 1.0 if (ctx.is_special_menu if ctx else service.is_special_menu) else 0.0,
            "menu_popularity": popularity,
            "dish_count": len(items),
        }
        _one_hot(row, service.meal.name, "meal", MEALS)
        rows.append(row)
    df = pd.DataFrame(rows).sort_values(["date", "meal_order"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    for meal in MEALS:
        mask = df["meal"] == meal
        g = df.loc[mask].sort_values("date")
        idx = g.index
        df.loc[idx, "lag_1"] = g["actual_participants"].shift(1)
        df.loc[idx, "lag_7"] = g["actual_participants"].shift(7)
        df.loc[idx, "lag_14"] = g["actual_participants"].shift(14)
        df.loc[idx, "rolling_mean_7"] = (
            g["actual_participants"].shift(1).rolling(7, min_periods=1).mean()
        )
        df.loc[idx, "rolling_mean_14"] = (
            g["actual_participants"].shift(1).rolling(14, min_periods=1).mean()
        )
    df["rolling_mean_7"] = df["rolling_mean_7"].fillna(df["actual_participants"].median())
    df["rolling_mean_14"] = df["rolling_mean_14"].fillna(df["rolling_mean_7"])
    df["lag_1"] = df["lag_1"].fillna(df["rolling_mean_7"])
    df["lag_7"] = df["lag_7"].fillna(df["rolling_mean_7"])
    df["lag_14"] = df["lag_14"].fillna(df["rolling_mean_14"])
    df["intent_ratio"] = df["declared_intents"] / df["rolling_mean_7"].clip(lower=1.0)
    df["has_intents"] = (df["declared_intents"] > 0).astype(float)
    return df, PARTICIPATION_FEATURES


def dish_training_frame() -> tuple[pd.DataFrame, list[str]]:
    menus = _menu_map()
    logs = list(WasteLog.objects.select_related("meal", "dish").order_by("date", "meal__order"))
    if not logs:
        return pd.DataFrame(columns=DISH_FEATURES + ["consumed_kg", "date"]), DISH_FEATURES
    participants = {
        (s.date, s.meal_id): s.actual_participants
        for s in MealService.objects.all().only("date", "meal_id", "actual_participants")
    }
    meal_totals: dict[tuple[dt.date, int], float] = defaultdict(float)
    for log in logs:
        meal_totals[(log.date, log.meal_id)] += float(log.consumed_kg)
    rows = []
    for log in logs:
        total = meal_totals[(log.date, log.meal_id)]
        if total <= 0:
            continue
        menu = menus.get((log.date, log.meal_id))
        items = list(menu.items.all()) if menu else []
        item = next((i for i in items if i.dish_id == log.dish_id), None)
        pop_sum = sum(i.dish.popularity_baseline for i in items) or 1.0
        row = {
            "date": log.date,
            "meal": log.meal.name,
            "meal_order": log.meal.order,
            "dish_id": log.dish_id,
            "dish_name": log.dish.name,
            "participants": participants.get((log.date, log.meal_id), 0),
            "consumed_kg": float(log.consumed_kg),
            "share": float(log.consumed_kg) / total,
            "dish_id": float(log.dish_id),
            "dish_weekday": float(log.dish_id * 7 + log.date.weekday()),
            "weekday": log.date.weekday(),
            "is_weekend": 1.0 if log.date.weekday() >= 5 else 0.0,
            "is_special": 1.0 if (item and item.is_special) else 0.0,
            "dish_count": len(items),
            "avg_portion_grams": float(log.dish.avg_portion_grams),
            "popularity": float(log.dish.popularity_baseline),
            "relative_popularity": float(log.dish.popularity_baseline) / pop_sum,
            "menu_popularity_sum": float(pop_sum),
            "weekday_x_popularity": float(log.date.weekday()) * float(log.dish.popularity_baseline),
            "meal_popularity_sum": float(pop_sum),
        }
        _one_hot(row, log.meal.name, "meal", MEALS)
        _one_hot(row, log.dish.category, "category", CATEGORIES)
        _one_hot(row, log.dish.diet_type, "diet", DIETS)
        rows.append(row)
    df = pd.DataFrame(rows).sort_values(["meal", "dish_id", "date"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    grouped = df.groupby(["meal", "dish_id"], sort=False)
    df["lag_1_share"] = grouped["share"].shift(1)
    df["lag_2_share"] = grouped["share"].shift(2)
    df["lag_3_share"] = grouped["share"].shift(3)
    df["lag_7_share"] = grouped["share"].shift(7)
    df["rolling_7_share"] = grouped["share"].transform(
        lambda s: s.shift(1).rolling(7, min_periods=1).mean()
    )
    df["rolling_14_share"] = grouped["share"].transform(
        lambda s: s.shift(1).rolling(14, min_periods=1).mean()
    )
    df["share_std_7"] = grouped["share"].transform(
        lambda s: s.shift(1).rolling(7, min_periods=2).std()
    )
    fallback = df["relative_popularity"].replace(0, 0.01)
    df["lag_1_share"] = df["lag_1_share"].fillna(fallback)
    df["lag_2_share"] = df["lag_2_share"].fillna(df["lag_1_share"])
    df["lag_3_share"] = df["lag_3_share"].fillna(df["lag_2_share"])
    df["lag_7_share"] = df["lag_7_share"].fillna(df["lag_1_share"])
    df["rolling_7_share"] = df["rolling_7_share"].fillna(df["lag_1_share"])
    df["rolling_14_share"] = df["rolling_14_share"].fillna(df["rolling_7_share"])
    df["share_std_7"] = df["share_std_7"].fillna(0.0)
    return df, DISH_FEATURES


def _menu_popularity(menu: DailyMenu | None) -> tuple[float, int, float]:
    items = list(menu.items.all()) if menu else []
    if not items:
        return 0.5, 0, 1.0
    pops = [float(i.dish.popularity_baseline) for i in items]
    return float(np.mean(pops)), len(items), float(sum(pops) or 1.0)


def participation_feature_row(
    date: dt.date, slot: MealSlot, declared_intents: int
) -> pd.DataFrame:
    history = list(
        MealService.objects.filter(meal=slot, date__lt=date)
        .order_by("-date")
        .values("date", "actual_participants", "declared_intents")
    )
    first_date = (
        MealService.objects.order_by("date").values_list("date", flat=True).first() or date
    )
    ctx = DayContext.objects.filter(date=date).first()
    menu = DailyMenu.objects.filter(date=date, meal=slot).prefetch_related("items__dish").first()
    popularity, dish_count, _pop_sum = _menu_popularity(menu)
    actuals = [h["actual_participants"] for h in history]
    by_date = {h["date"]: h["actual_participants"] for h in history}

    def lag(days: int):
        value = by_date.get(date - dt.timedelta(days=days))
        return float(value) if value is not None else float(np.mean(actuals[:7])) if actuals else 0.0

    rolling7 = float(np.mean(actuals[:7])) if actuals else 0.0
    rolling14 = float(np.mean(actuals[:14])) if actuals else 0.0
    weather = ctx.weather if ctx else "clear"
    row = {
        "weekday": float(date.weekday()),
        "is_weekend": 1.0 if date.weekday() >= 5 else 0.0,
        "week_of_semester": float((date - first_date).days // 7),
        "is_holiday": 1.0 if (ctx and ctx.is_holiday) else 0.0,
        "is_exam": 1.0 if (ctx and ctx.is_exam) else 0.0,
        "is_rain": 1.0 if weather == "rain" else 0.0,
        "is_hot": 1.0 if weather == "hot" else 0.0,
        "is_cold": 1.0 if weather == "cold" else 0.0,
        "has_event": 1.0 if (ctx and ctx.event_name) else 0.0,
        "is_special_menu": 1.0 if (ctx and ctx.is_special_menu) else 0.0,
        "menu_popularity": popularity,
        "dish_count": float(dish_count),
        "lag_1": lag(1),
        "lag_7": lag(7),
        "lag_14": lag(14),
        "rolling_mean_7": rolling7,
        "rolling_mean_14": rolling14,
        "declared_intents": float(declared_intents),
        "intent_ratio": float(declared_intents) / max(rolling7, 1.0),
        "has_intents": 1.0 if declared_intents > 0 else 0.0,
    }
    _one_hot(row, slot.name, "meal", MEALS)
    return pd.DataFrame([row])[PARTICIPATION_FEATURES]


def dish_feature_rows(
    date: dt.date, slot: MealSlot, menu: DailyMenu, participants: int
) -> pd.DataFrame:
    items = list(menu.items.all())
    if not items:
        return pd.DataFrame(columns=DISH_FEATURES)
    pop_sum = sum(i.dish.popularity_baseline for i in items) or 1.0
    logs = list(
        WasteLog.objects.filter(meal=slot, date__lt=date)
        .select_related("dish")
        .order_by("date")
    )
    meal_totals: dict[dt.date, float] = defaultdict(float)
    for log in logs:
        meal_totals[log.date] += float(log.consumed_kg)
    history: dict[int, list[float]] = defaultdict(list)
    for log in logs:
        total = meal_totals.get(log.date, 0)
        if total > 0:
            history[log.dish_id].append(float(log.consumed_kg) / total)
    rows = []
    for item in items:
        dish: Dish = item.dish
        shares = history.get(dish.pk, [])
        fallback = dish.popularity_baseline / pop_sum
        lag1 = shares[-1] if shares else fallback
        lag2 = shares[-2] if len(shares) >= 2 else lag1
        lag3 = shares[-3] if len(shares) >= 3 else lag2
        lag7 = shares[-7] if len(shares) >= 7 else lag1
        rolling = float(np.mean(shares[-7:])) if shares else fallback
        rolling14 = float(np.mean(shares[-14:])) if shares else fallback
        recent = shares[-7:]
        share_std = float(np.std(recent)) if len(recent) >= 2 else 0.0
        row = {
            "dish_id": float(dish.pk),
            "dish_name": dish.name,
            "dish_weekday": float(dish.pk * 7 + date.weekday()),
            "weekday": float(date.weekday()),
            "is_weekend": 1.0 if date.weekday() >= 5 else 0.0,
            "is_special": 1.0 if item.is_special else 0.0,
            "dish_count": float(len(items)),
            "avg_portion_grams": float(dish.avg_portion_grams),
            "popularity": float(dish.popularity_baseline),
            "relative_popularity": float(dish.popularity_baseline) / pop_sum,
            "menu_popularity_sum": float(pop_sum),
            "weekday_x_popularity": float(date.weekday()) * float(dish.popularity_baseline),
            "lag_1_share": lag1,
            "lag_2_share": lag2,
            "lag_3_share": lag3,
            "lag_7_share": lag7,
            "rolling_7_share": rolling,
            "rolling_14_share": rolling14,
            "share_std_7": share_std,
            "meal_popularity_sum": float(pop_sum),
        }
        _one_hot(row, slot.name, "meal", MEALS)
        _one_hot(row, dish.category, "category", CATEGORIES)
        _one_hot(row, dish.diet_type, "diet", DIETS)
        rows.append(row)
    return pd.DataFrame(rows)
