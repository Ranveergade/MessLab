"""Operational intelligence: waste, impact, crowd, peaks, menu and live status."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from django.conf import settings
from django.db.models import Avg, Count, F, Sum
from django.utils import timezone

from dining.models import (
    CheckIn,
    Forecast,
    MealFeedback,
    MealIntent,
    MealService,
    PreparationPlan,
    WasteLog,
)
from dining.services import (
    MEAL_BASE_RATE,
    declared_positive_intents,
    describe_intent,
    intent_counts,
    resolve_default_intent,
    today,
)
from menu.models import DailyMenu, Dish, MealSlot
from rewards.services import student_summary


def assumptions() -> dict:
    return {
        "cost_per_kg_inr": settings.MESSLAB["COST_PER_KG_INR"],
        "co2e_per_kg": settings.MESSLAB["CO2E_PER_KG"],
        "service_level": settings.MESSLAB["SERVICE_LEVEL"],
        "shortage_threshold": settings.MESSLAB["SHORTAGE_THRESHOLD"],
        "avg_kg_per_student": settings.MESSLAB["AVG_KG_PER_STUDENT"],
        "buffer_min_pct": settings.MESSLAB["MIN_BUFFER_PCT"],
        "buffer_max_pct": settings.MESSLAB["MAX_BUFFER_PCT"],
        "disclaimer": "All figures are computed from the mess records stored in this database.",
    }


def _range(days: int, end_date: dt.date | None = None) -> tuple[dt.date, dt.date]:
    end = end_date or today()
    return end - dt.timedelta(days=days - 1), end


def waste_analytics(days: int = 30, end_date: dt.date | None = None) -> dict:
    start, end = _range(days, end_date)
    logs = list(
        WasteLog.objects.filter(date__gte=start, date__lte=end)
        .select_related("meal", "dish")
        .order_by("date", "meal__order")
    )
    if not logs:
        return {
            "start": start,
            "end": end,
            "days": days,
            "empty": True,
            "totals": {"prepared_kg": 0, "consumed_kg": 0, "wasted_kg": 0, "waste_pct": 0,
                       "cost_inr": 0, "co2e_kg": 0, "reusable_batches": 0, "shortage_incidents": 0},
            "by_meal": [],
            "by_dish": [],
            "trend": [],
            "assumptions": assumptions(),
        }
    prepared = sum(float(l.prepared_kg) for l in logs)
    consumed = sum(float(l.consumed_kg) for l in logs)
    wasted = sum(float(l.wasted_kg) for l in logs)
    reusable = sum(1 for l in logs if l.leftover_reusable)
    shortage = sum(
        1
        for l in logs
        if float(l.prepared_kg) > 0
        and float(l.consumed_kg) >= float(l.prepared_kg) * settings.MESSLAB["SHORTAGE_THRESHOLD"]
    )
    meal_map: dict[str, dict] = {}
    for log in logs:
        row = meal_map.setdefault(
            log.meal.name,
            {"meal": log.meal.name, "label": log.meal.get_name_display(), "prepared": 0.0,
             "consumed": 0.0, "wasted": 0.0, "entries": 0},
        )
        row["prepared"] += float(log.prepared_kg)
        row["consumed"] += float(log.consumed_kg)
        row["wasted"] += float(log.wasted_kg)
        row["entries"] += 1
    by_meal = []
    for row in meal_map.values():
        row["waste_pct"] = round(row["wasted"] / row["prepared"] * 100, 1) if row["prepared"] else 0
        row["wasted"] = round(row["wasted"], 1)
        row["prepared"] = round(row["prepared"], 1)
        row["consumed"] = round(row["consumed"], 1)
        by_meal.append(row)
    by_meal.sort(key=lambda r: r["wasted"], reverse=True)

    dish_map: dict[int, dict] = {}
    for log in logs:
        row = dish_map.setdefault(
            log.dish_id,
            {"dish": log.dish.name, "category": log.dish.get_category_display(),
             "prepared": 0.0, "consumed": 0.0, "wasted": 0.0, "served": 0,
             "reusable": 0},
        )
        row["prepared"] += float(log.prepared_kg)
        row["consumed"] += float(log.consumed_kg)
        row["wasted"] += float(log.wasted_kg)
        row["served"] += 1
        row["reusable"] += 1 if log.leftover_reusable else 0
    by_dish = []
    for row in dish_map.values():
        row["waste_pct"] = round(row["wasted"] / row["prepared"] * 100, 1) if row["prepared"] else 0
        row["wasted"] = round(row["wasted"], 1)
        row["prepared"] = round(row["prepared"], 1)
        row["cost_inr"] = round(row["wasted"] * settings.MESSLAB["COST_PER_KG_INR"])
        by_dish.append(row)
    by_dish.sort(key=lambda r: r["wasted"], reverse=True)

    trend_map: dict[dt.date, dict] = {}
    for log in logs:
        row = trend_map.setdefault(log.date, {"date": log.date, "prepared": 0.0, "wasted": 0.0})
        row["prepared"] += float(log.prepared_kg)
        row["wasted"] += float(log.wasted_kg)
    trend = []
    for row in sorted(trend_map.values(), key=lambda r: r["date"]):
        trend.append(
            {
                "date": row["date"],
                "prepared": round(row["prepared"], 1),
                "wasted": round(row["wasted"], 1),
                "waste_pct": round(row["wasted"] / row["prepared"] * 100, 1) if row["prepared"] else 0,
            }
        )
    return {
        "start": start,
        "end": end,
        "days": days,
        "empty": False,
        "totals": {
            "prepared_kg": round(prepared, 1),
            "consumed_kg": round(consumed, 1),
            "wasted_kg": round(wasted, 1),
            "waste_pct": round(wasted / prepared * 100, 1) if prepared else 0,
            "cost_inr": round(wasted * settings.MESSLAB["COST_PER_KG_INR"]),
            "co2e_kg": round(wasted * settings.MESSLAB["CO2E_PER_KG"], 1),
            "reusable_batches": reusable,
            "shortage_incidents": shortage,
            "entries": len(logs),
        },
        "by_meal": by_meal,
        "by_dish": by_dish[:10],
        "trend": trend,
        "assumptions": assumptions(),
    }


def impact_summary(days: int = 30) -> dict:
    start, end = _range(days)
    cutoff = today()
    plans = list(
        PreparationPlan.objects.filter(date__gte=start, date__lte=min(end, cutoff - dt.timedelta(days=1)))
        .select_related("meal", "dish")
        .order_by("date", "meal__order")
    )
    logs = {
        (l.date, l.meal_id, l.dish_id): l
        for l in WasteLog.objects.filter(date__gte=start, date__lte=end)
    }
    matched = 0
    without_waste = 0.0
    with_waste = 0.0
    without_shortage = 0
    with_shortage = 0
    consumed_total = 0.0
    baseline_total = 0.0
    recommended_total = 0.0
    meal_keys = set()
    threshold = settings.MESSLAB["SHORTAGE_THRESHOLD"]
    for plan in plans:
        log = logs.get((plan.date, plan.meal_id, plan.dish_id))
        if not log:
            continue
        matched += 1
        meal_keys.add((plan.date, plan.meal_id))
        consumed = float(log.consumed_kg)
        baseline = float(plan.baseline_kg) or float(log.prepared_kg)
        recommended = float(plan.recommended_kg)
        consumed_total += consumed
        baseline_total += baseline
        recommended_total += recommended
        without_waste += max(0.0, baseline - consumed)
        with_waste += max(0.0, recommended - consumed)
        if baseline > 0 and consumed >= baseline * threshold:
            without_shortage += 1
        if recommended > 0 and consumed >= recommended * threshold:
            with_shortage += 1
    participants = 0
    if meal_keys:
        dates = {k[0] for k in meal_keys}
        meal_ids = {k[1] for k in meal_keys}
        participants = (
            MealService.objects.filter(date__in=dates, meal_id__in=meal_ids).aggregate(
                total=Sum("actual_participants")
            )["total"]
            or 0
        )
    avoided = without_waste - with_waste
    cost = settings.MESSLAB["COST_PER_KG_INR"]
    co2 = settings.MESSLAB["CO2E_PER_KG"]
    return {
        "start": start,
        "end": end,
        "days": days,
        "matched_rows": matched,
        "meals": len(meal_keys),
        "participants": participants,
        "without": {
            "waste_kg": round(without_waste, 1),
            "cost_inr": round(without_waste * cost),
            "co2e_kg": round(without_waste * co2, 1),
            "shortage_incidents": without_shortage,
            "prepared_kg": round(baseline_total, 1),
        },
        "with": {
            "waste_kg": round(with_waste, 1),
            "cost_inr": round(with_waste * cost),
            "co2e_kg": round(with_waste * co2, 1),
            "shortage_incidents": with_shortage,
            "prepared_kg": round(recommended_total, 1),
        },
        "avoided": {
            "waste_kg": round(avoided, 1),
            "cost_inr": round(avoided * cost),
            "co2e_kg": round(avoided * co2, 1),
            "prepared_kg": round(baseline_total - recommended_total, 1),
            "shortage_delta": without_shortage - with_shortage,
            "waste_pct": round(avoided / without_waste * 100, 1) if without_waste else 0.0,
        },
        "consumed_kg": round(consumed_total, 1),
        "assumptions": assumptions(),
        "empty": matched == 0,
    }


def _buckets(slot: MealSlot) -> list[dict]:
    start = slot.start_time.hour * 60 + slot.start_time.minute
    end = slot.end_time.hour * 60 + slot.end_time.minute
    out = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + 30, end)
        out.append(
            {
                "start_min": cursor,
                "end_min": nxt,
                "label": f"{cursor // 60:02d}:{cursor % 60:02d}–{nxt // 60:02d}:{nxt % 60:02d}",
                "capacity": slot.hourly_capacity / 2,
            }
        )
        cursor = nxt
    return out


def _arrival_distribution(date: dt.date, slot: MealSlot, lookback_days: int = 28) -> list[float]:
    buckets = _buckets(slot)
    if not buckets:
        return []
    since = date - dt.timedelta(days=lookback_days)
    rows = list(
        CheckIn.objects.filter(meal=slot, date__gte=since, date__lt=date).values_list(
            "timestamp", flat=True
        )
    )
    counts = [0.0] * len(buckets)
    for ts in rows:
        local = timezone.localtime(ts)
        minutes = local.hour * 60 + local.minute
        for idx, bucket in enumerate(buckets):
            if bucket["start_min"] <= minutes < bucket["end_min"]:
                counts[idx] += 1
                break
    total = sum(counts)
    if total:
        return [c / total for c in counts]
    # Sensible default: 20% early, 55% around the peak, 25% late.
    weights = []
    peak_start = slot.peak_start.hour * 60 + slot.peak_start.minute
    peak_end = slot.peak_end.hour * 60 + slot.peak_end.minute
    for bucket in buckets:
        mid = (bucket["start_min"] + bucket["end_min"]) / 2
        if peak_start <= mid <= peak_end:
            weights.append(3.0)
        elif mid < peak_start:
            weights.append(1.0)
        else:
            weights.append(1.4)
    total_w = sum(weights)
    return [w / total_w for w in weights]


def _intent_distribution(date: dt.date, slot: MealSlot) -> list[float] | None:
    buckets = _buckets(slot)
    if not buckets:
        return None
    counts = {a: 0 for a in MealIntent.Arrival.values}
    rows = (
        MealIntent.objects.filter(
            date=date, meal=slot, status__in=[MealIntent.Status.YES, MealIntent.Status.MAYBE]
        )
        .values("expected_arrival_window")
        .annotate(n=Count("id"))
    )
    for row in rows:
        counts[row["expected_arrival_window"]] = row["n"]
    total = sum(counts.values())
    if total == 0:
        return None
    weights = [0.0] * len(buckets)
    peak_start = slot.peak_start.hour * 60 + slot.peak_start.minute
    peak_end = slot.peak_end.hour * 60 + slot.peak_end.minute
    for idx, bucket in enumerate(buckets):
        mid = (bucket["start_min"] + bucket["end_min"]) / 2
        if mid < peak_start:
            weights[idx] = counts["early"] / max(1, sum(1 for b in buckets if (b["start_min"] + b["end_min"]) / 2 < peak_start))
        elif mid <= peak_end:
            weights[idx] = counts["peak"] / max(1, sum(1 for b in buckets if peak_start <= (b["start_min"] + b["end_min"]) / 2 <= peak_end))
        else:
            weights[idx] = counts["late"] / max(1, sum(1 for b in buckets if (b["start_min"] + b["end_min"]) / 2 > peak_end))
    total_w = sum(weights)
    return [w / total_w for w in weights] if total_w else None


def crowd_forecast(date: dt.date, slot: MealSlot) -> dict:
    buckets = _buckets(slot)
    forecast = Forecast.objects.filter(date=date, meal=slot, dish=None).first()
    participants = forecast.predicted_participation if forecast else 0
    if not participants:
        intents = declared_positive_intents(date, slot)
        participants = intents or int(
            (StudentProfile_count() or 1500) * MEAL_BASE_RATE.get(slot.name, 0.7)
        )
    base = _arrival_distribution(date, slot)
    intent_dist = _intent_distribution(date, slot)
    if intent_dist:
        dist = [0.65 * b + 0.35 * i for b, i in zip(base, intent_dist)]
    else:
        dist = base
    total = sum(dist) or 1.0
    dist = [d / total for d in dist]
    rows = []
    for bucket, share in zip(buckets, dist):
        expected = participants * share
        capacity = max(1.0, bucket["capacity"])
        risk = expected / capacity
        level = "low" if risk < 0.6 else "medium" if risk < 0.9 else "high"
        rows.append(
            {
                "label": bucket["label"],
                "expected": int(round(expected)),
                "capacity": int(round(capacity)),
                "risk": round(risk, 2),
                "level": level,
                "share": round(share, 3),
            }
        )
    eligible = [r for r in rows if r["share"] >= 0.04] or rows
    best = min(eligible, key=lambda r: r["risk"]) if eligible else None
    peak_risk = max((r["risk"] for r in rows), default=0)
    overall = "low" if peak_risk < 0.6 else "medium" if peak_risk < 0.9 else "high"
    return {
        "date": date,
        "meal": slot,
        "participants": participants,
        "buckets": rows,
        "best_time": best["label"].split("–")[0] if best else "—",
        "best_window": best["label"] if best else "—",
        "overall_level": overall,
        "peak_risk": round(peak_risk, 2),
        "source": "forecast" if forecast else "intent/fallback",
    }


def StudentProfile_count() -> int:
    from accounts.models import StudentProfile

    return StudentProfile.objects.count()


def peak_analysis(date: dt.date, slot: MealSlot, lookback_days: int = 28) -> dict:
    since = date - dt.timedelta(days=lookback_days)
    checkins = list(
        CheckIn.objects.filter(meal=slot, date__gte=since, date__lt=date)
        .select_related("student__profile")
        .values("timestamp", "student__profile__hostel")
    )
    buckets = _buckets(slot)
    counts = [0] * len(buckets)
    hostel_counts: dict[str, int] = defaultdict(int)
    heat: dict[int, list[int]] = {d: [0] * len(buckets) for d in range(7)}
    for row in checkins:
        local = timezone.localtime(row["timestamp"])
        minutes = local.hour * 60 + local.minute
        for idx, bucket in enumerate(buckets):
            if bucket["start_min"] <= minutes < bucket["end_min"]:
                counts[idx] += 1
                heat[local.weekday()][idx] += 1
                break
        if row["student__profile__hostel"]:
            hostel_counts[row["student__profile__hostel"]] += 1
    days_count = max(1, lookback_days)
    avg_counts = [c / days_count for c in counts]
    capacity = max(1.0, slot.hourly_capacity / 2)
    rows = []
    for bucket, avg in zip(buckets, avg_counts):
        risk = avg / capacity
        rows.append(
            {
                "label": bucket["label"],
                "avg_arrivals": round(avg, 1),
                "capacity": int(capacity),
                "risk": round(risk, 2),
                "level": "low" if risk < 0.6 else "medium" if risk < 0.9 else "high",
            }
        )
    max_risk = max((r["risk"] for r in rows), default=0)
    extra = 0
    if max_risk > 0.9:
        peak_row = max(rows, key=lambda r: r["risk"])
        extra = max(1, math.ceil((peak_row["avg_arrivals"] - capacity) / capacity))
    top_hostels = sorted(hostel_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    total = sum(hostel_counts.values()) or 1
    heatmap = []
    for day in range(7):
        heatmap.append(
            {
                "weekday": day,
                "label": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day],
                "cells": [
                    {
                        "label": bucket["label"],
                        "count": round(heat[day][idx] / max(1, sum(1 for _ in range(4))), 1),
                        "level": "low"
                        if heat[day][idx] / (4 * capacity) < 0.6
                        else "medium"
                        if heat[day][idx] / (4 * capacity) < 0.9
                        else "high",
                    }
                    for idx, bucket in enumerate(buckets)
                ],
            }
        )
    return {
        "date": date,
        "meal": slot,
        "buckets": rows,
        "max_risk": round(max_risk, 2),
        "suggest_extra_counters": extra,
        "top_hostels": [
            {"hostel": h, "share": round(n / total * 100, 1)} for h, n in top_hostels
        ],
        "heatmap": heatmap,
        "heatmap_labels": [b["label"] for b in buckets],
        "lookback_days": lookback_days,
    }


def live_meal_status(date: dt.date, slot: MealSlot) -> dict:
    counts = intent_counts(date, slot)
    checkins = CheckIn.objects.filter(date=date, meal=slot).count()
    forecast = Forecast.objects.filter(date=date, meal=slot, dish=None).first()
    predicted = forecast.predicted_participation if forecast else counts["POSITIVE"]
    plans = list(
        PreparationPlan.objects.filter(date=date, meal=slot)
        .select_related("dish")
        .order_by("dish__name")
    )
    dish_forecasts = {
        f.dish_id: f
        for f in Forecast.objects.filter(date=date, meal=slot, dish__isnull=False)
    }
    total_predicted_demand = sum(
        float(f.predicted_demand_kg) for f in dish_forecasts.values()
    ) or 1.0
    avg_kg = settings.MESSLAB["AVG_KG_PER_STUDENT"]
    expected_remaining = max(0, predicted - checkins)
    dishes = []
    alerts = []
    for plan in plans:
        fc = dish_forecasts.get(plan.dish_id)
        share = float(fc.predicted_demand_kg) / total_predicted_demand if fc else 0.2
        prepared = float(plan.actual_prepared_kg or plan.recommended_kg)
        consumed_est = min(prepared, checkins * avg_kg * share)
        remaining = max(0.0, prepared - consumed_est)
        needed = expected_remaining * avg_kg * share
        deficit = max(0.0, needed - remaining)
        runout = remaining < needed * 1.02 and needed > 0.2
        dishes.append(
            {
                "plan_id": plan.pk,
                "dish": plan.dish.name,
                "prepared_kg": round(prepared, 1),
                "consumed_est_kg": round(consumed_est, 1),
                "remaining_kg": round(remaining, 1),
                "expected_need_kg": round(needed, 1),
                "share_pct": round(share * 100, 1),
                "runout_risk": runout,
                "suggested_extra_kg": round(math.ceil(deficit * 2) / 2, 1) if runout else 0.0,
            }
        )
        if runout:
            alerts.append(
                {
                    "severity": "high" if deficit > 2 else "medium",
                    "dish": plan.dish.name,
                    "message": (
                        f"Prepare {math.ceil(deficit * 2) / 2:.1f} kg more {plan.dish.name.lower()} "
                        f"in the next 15 min."
                    ),
                }
            )
    recent = CheckIn.objects.filter(
        date=date, meal=slot, timestamp__gte=timezone.now() - dt.timedelta(minutes=30)
    ).count()
    capacity_30 = max(1, slot.hourly_capacity // 2)
    risk = recent / capacity_30
    level = "low" if risk < 0.6 else "medium" if risk < 0.9 else "high"
    if not dishes:
        alerts.append(
            {
                "severity": "info",
                "dish": "",
                "message": "No preparation plan yet. Generate a forecast for this date.",
            }
        )
    return {
        "date": date,
        "meal": slot,
        "intents": counts,
        "checkins": checkins,
        "predicted_participants": predicted,
        "attendance_pct": round(checkins / predicted * 100, 1) if predicted else 0,
        "remaining_expected": expected_remaining,
        "dishes": dishes,
        "alerts": alerts,
        "recent_30min": recent,
        "crowd_level": level,
        "crowd_risk": round(risk, 2),
        "has_plan": bool(plans),
    }


def menu_insights(days: int = 30) -> dict:
    start, end = _range(days)
    logs = list(
        WasteLog.objects.filter(date__gte=start, date__lte=end)
        .select_related("meal", "dish")
        .order_by("date")
    )
    services = {
        (s.date, s.meal_id): s.actual_participants
        for s in MealService.objects.filter(date__gte=start, date__lte=end)
    }
    dish_map: dict[int, dict] = {}
    for log in logs:
        participants = services.get((log.date, log.meal_id), 0)
        row = dish_map.setdefault(
            log.dish_id,
            {
                "dish": log.dish.name,
                "category": log.dish.get_category_display(),
                "diet": log.dish.get_diet_type_display(),
                "served": 0,
                "consumed": 0.0,
                "prepared": 0.0,
                "wasted": 0.0,
                "shortages": 0,
                "participant_kg": 0.0,
                "portion_grams": log.dish.avg_portion_grams,
            },
        )
        row["served"] += 1
        row["consumed"] += float(log.consumed_kg)
        row["prepared"] += float(log.prepared_kg)
        row["wasted"] += float(log.wasted_kg)
        row["participant_kg"] += participants * (log.dish.avg_portion_grams / 1000)
        if float(log.prepared_kg) > 0 and float(log.consumed_kg) >= float(log.prepared_kg) * settings.MESSLAB["SHORTAGE_THRESHOLD"]:
            row["shortages"] += 1
    rows = []
    for row in dish_map.values():
        uptake = row["consumed"] / row["participant_kg"] if row["participant_kg"] else 0
        waste_pct = row["wasted"] / row["prepared"] * 100 if row["prepared"] else 0
        rows.append(
            {
                "dish": row["dish"],
                "category": row["category"],
                "diet": row["diet"],
                "served": row["served"],
                "consumed_kg": round(row["consumed"], 1),
                "wasted_kg": round(row["wasted"], 1),
                "waste_pct": round(waste_pct, 1),
                "uptake_pct": round(min(1.5, uptake) * 100, 1),
                "shortages": row["shortages"],
                "portion_grams": row["portion_grams"],
            }
        )
    rotate = [
        r for r in rows if r["served"] >= 3 and (r["uptake_pct"] < 40 or r["waste_pct"] > 18)
    ]
    rotate.sort(key=lambda r: (r["uptake_pct"], -r["wasted_kg"]))
    shortage = [r for r in rows if r["shortages"] >= 2]
    shortage.sort(key=lambda r: r["shortages"], reverse=True)
    stars = sorted(rows, key=lambda r: r["uptake_pct"], reverse=True)[:5]
    return {
        "start": start,
        "end": end,
        "days": days,
        "rows": sorted(rows, key=lambda r: r["wasted_kg"], reverse=True),
        "rotate": rotate[:8],
        "shortage": shortage[:8],
        "stars": stars,
        "empty": not rows,
    }


def model_health() -> dict:
    from forecasting.models import ModelRun

    run = ModelRun.objects.filter(is_active=True).order_by("-trained_at").first()
    if not run:
        run = ModelRun.objects.order_by("-trained_at").first()
    return {
        "run": run,
        "runs": list(ModelRun.objects.all()[:8]),
        "empty": run is None,
        "assumptions": assumptions(),
    }


def nudge_for(student, date: dt.date, slot: MealSlot, menu: DailyMenu | None, crowd: dict) -> dict:
    if not menu:
        return {"kind": "info", "text": "Menu is being finalised for this meal."}
    items = list(menu.items.all())
    favourite = None
    from dining.models import CheckInDish

    counts = (
        CheckInDish.objects.filter(check_in__student=student, dish__in=[i.dish for i in items])
        .values("dish__name")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )
    if counts:
        favourite = counts["dish__name"]
    else:
        favourite = max(items, key=lambda i: i.dish.popularity_baseline).dish.name
    when = crowd.get("best_window") or "a quieter window"
    if crowd.get("overall_level") == "high":
        prefix = f"Tonight's {slot.name}: {favourite} is on the menu. It will be busy."
        return {
            "kind": "crowd",
            "text": f"{prefix} Go at {when} for the shortest queue.",
        }
    if favourite:
        return {
            "kind": "favourite",
            "text": f"{slot.name.title()}: {favourite} is on today. Quietest around {when}.",
        }
    return {"kind": "info", "text": f"{slot.name.title()} is served {slot.start_time:%H:%M}–{slot.end_time:%H:%M}."}


def today_meals(student, date: dt.date | None = None) -> list[dict]:
    date = date or today()
    slots = list(MealSlot.objects.all())
    menus = {
        menu.meal_id: menu
        for menu in DailyMenu.objects.filter(date=date).prefetch_related("items__dish")
    }
    intents = {
        intent.meal_id: intent
        for intent in MealIntent.objects.filter(student=student, date=date)
    }
    checkins = {
        checkin.meal_id: checkin
        for checkin in CheckIn.objects.filter(student=student, date=date)
    }
    feedback = {
        row.meal_id: row
        for row in MealFeedback.objects.filter(student=student, date=date)
    }
    out = []
    profile = getattr(student, "profile", None)
    for slot in slots:
        menu = menus.get(slot.pk)
        if not menu:
            out.append(
                {
                    "slot": slot,
                    "menu": None,
                    "items": [],
                    "crowd": None,
                    "intent": None,
                    "checkin": None,
                    "feedback": None,
                    "nudge": {"kind": "empty", "text": "No menu published yet."},
                    "allergy_note": "",
                }
            )
            continue
        crowd = crowd_forecast(date, slot)
        items = list(menu.items.all())
        allergy_note = ""
        if profile and profile.allergies:
            tokens = [t.strip().lower() for t in profile.allergies.replace(";", ",").split(",") if t.strip()]
            hits = [i.dish.name for i in items if any(t in i.dish.name.lower() for t in tokens)]
            if hits:
                allergy_note = "Check ingredients — you listed: " + ", ".join(tokens)
        intent = intents.get(slot.pk)
        if intent is not None:
            intent_view = describe_intent(student, date, slot, intent)
        else:
            intent_view = resolve_default_intent(student, date, slot)
        out.append(
            {
                "slot": slot,
                "menu": menu,
                "items": items,
                "crowd": crowd,
                "intent": intent_view,
                "checkin": checkins.get(slot.pk),
                "feedback": feedback.get(slot.pk),
                "nudge": nudge_for(student, date, slot, menu, crowd),
                "allergy_note": allergy_note,
                "diet_allows": {i.dish_id: diet_allows_for(profile, i.dish) for i in items},
            }
        )
    return out


def diet_allows_for(profile, dish: Dish) -> bool:
    from dining.services import diet_allows

    return diet_allows(profile, dish)


def dashboard_context(date: dt.date | None = None) -> dict:
    date = date or today()
    meals = []
    total_recommended = 0.0
    total_baseline = 0.0
    for slot in MealSlot.objects.all():
        plans = list(
            PreparationPlan.objects.filter(date=date, meal=slot)
            .select_related("dish")
            .order_by("dish__name")
        )
        forecasts = {
            f.dish_id: f for f in Forecast.objects.filter(date=date, meal=slot, dish__isnull=False)
        }
        meal_forecast = Forecast.objects.filter(date=date, meal=slot, dish=None).first()
        rows = []
        for plan in plans:
            fc = forecasts.get(plan.dish_id)
            recommended = float(plan.recommended_kg)
            baseline = float(plan.baseline_kg)
            total_recommended += recommended
            total_baseline += baseline
            rows.append(
                {
                    "plan": plan,
                    "forecast": fc,
                    "recommended_kg": recommended,
                    "baseline_kg": baseline,
                    "saved_kg": max(0.0, baseline - recommended),
                }
            )
        meals.append(
            {
                "slot": slot,
                "rows": rows,
                "forecast": meal_forecast,
                "intents": intent_counts(date, slot),
                "crowd": crowd_forecast(date, slot),
                "approved": bool(rows) and all(r["plan"].is_approved for r in rows),
                "draft": any(r["plan"].status == PreparationPlan.Status.DRAFT for r in rows),
            }
        )
    return {
        "date": date,
        "meals": meals,
        "total_recommended": round(total_recommended, 1),
        "total_baseline": round(total_baseline, 1),
        "total_saved": round(max(0.0, total_baseline - total_recommended), 1),
        "waste": waste_analytics(days=14),
        "impact": impact_summary(days=30),
        "health": model_health(),
        "assumptions": assumptions(),
    }


def student_dashboard(student, date: dt.date | None = None) -> dict:
    from rewards.services import hostel_leaderboard

    date = date or today()
    return {
        "date": date,
        "meals": today_meals(student, date),
        "rewards": student_summary(student, date),
        "leaderboard": hostel_leaderboard(date),
        "assumptions": assumptions(),
    }
