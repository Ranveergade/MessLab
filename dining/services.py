"""Domain services shared by the web UI, API and management commands."""
from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from accounts.models import AccountAudit, StudentProfile
from menu.models import DailyMenu, Dish, MealSlot
from rewards.services import award_checkin_rewards, award_skip_notice

from .models import (
    CheckIn,
    CheckInDish,
    MealFeedback,
    MealIntent,
    MealService,
    PreparationPlan,
    WasteLog,
)

MEAL_BASE_RATE = {
    "breakfast": 0.62,
    "lunch": 0.88,
    "dinner": 0.84,
    "snacks": 0.48,
}
ARRIVAL_ORDER = ("early", "peak", "late")


# A test-only seam. No runtime code writes this; the test suite pins a date so
# fixtures and assertions can use a fixed calendar.
_TEST_TODAY: dt.date | None = None


def today() -> dt.date:
    return _TEST_TODAY or timezone.localdate()


def now() -> dt.datetime:
    return timezone.now()


def meal_window(date: dt.date, slot: MealSlot) -> tuple[dt.datetime, dt.datetime]:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(dt.datetime.combine(date, slot.start_time), tz)
    end = timezone.make_aware(dt.datetime.combine(date, slot.end_time), tz)
    return start, end


def meal_slots():
    return list(MealSlot.objects.all())


def menu_for(date: dt.date, meal: MealSlot) -> DailyMenu | None:
    return (
        DailyMenu.objects.filter(date=date, meal=meal)
        .prefetch_related("items__dish")
        .first()
    )


def _historical_arrival(student, date: dt.date, slot: MealSlot) -> str:
    rows = list(
        CheckIn.objects.filter(
            student=student, meal=slot, date__lt=date, date__gte=date - dt.timedelta(days=42)
        ).values_list("timestamp", flat=True)
    )
    if not rows:
        return MealIntent.Arrival.PEAK
    early_end = slot.start_time
    peak_end = slot.peak_end
    counts = Counter()
    for ts in rows:
        local = timezone.localtime(ts)
        minutes = local.hour * 60 + local.minute
        start_min = early_end.hour * 60 + early_end.minute
        peak_end_min = peak_end.hour * 60 + peak_end.minute
        if minutes < start_min + 30:
            counts["early"] += 1
        elif minutes <= peak_end_min:
            counts["peak"] += 1
        else:
            counts["late"] += 1
    return counts.most_common(1)[0][0] if counts else MealIntent.Arrival.PEAK


def _historical_attendance_ratio(student, date: dt.date, slot: MealSlot) -> float | None:
    since = date - dt.timedelta(days=28)
    weeks = 4.0
    attended = CheckIn.objects.filter(
        student=student, meal=slot, date__lt=date, date__gte=since
    ).count()
    if attended == 0:
        # Distinguish "never came" from "no data": use any older check-in as signal.
        if not CheckIn.objects.filter(student=student, meal=slot, date__lt=date).exists():
            return None
        return 0.0
    return min(1.0, attended / weeks)


def resolve_default_intent(student, date: dt.date, slot: MealSlot) -> dict:
    """The zero-input suggestion: weekly plan first, then learned habit."""
    profile = getattr(student, "profile", None)
    plan = profile.plan_for(date.weekday(), slot.name) if profile else "auto"
    if plan == "eat":
        status, source = MealIntent.Status.YES, MealIntent.Source.DEFAULT
    elif plan == "skip":
        status, source = MealIntent.Status.NO, MealIntent.Source.DEFAULT
    else:
        ratio = _historical_attendance_ratio(student, date, slot)
        if ratio is None:
            rate = MEAL_BASE_RATE.get(slot.name, 0.7)
            if slot.name == "breakfast" and date.weekday() == 6:
                rate = 0.2
            status = MealIntent.Status.YES if rate >= 0.5 else MealIntent.Status.NO
            source = MealIntent.Source.AUTO_INFERRED
        else:
            status = MealIntent.Status.YES if ratio >= 0.45 else MealIntent.Status.NO
            source = MealIntent.Source.AUTO_INFERRED
    return {
        "status": status,
        "source": source,
        "expected_arrival_window": _historical_arrival(student, date, slot),
        "plan_value": plan,
        "is_explicit": False,
    }


def describe_intent(student, date: dt.date, slot: MealSlot, intent: MealIntent) -> dict:
    """Serialise an existing intent the way the student pages expect."""
    profile = getattr(student, "profile", None)
    return {
        "id": intent.pk,
        "status": intent.status,
        "source": intent.source,
        "expected_arrival_window": intent.expected_arrival_window,
        "plan_value": profile.plan_for(date.weekday(), slot.name) if profile else "auto",
        "is_explicit": True,
        "updated_at": intent.updated_at,
    }


def get_intent_view(student, date: dt.date, slot: MealSlot) -> dict:
    intent = MealIntent.objects.filter(student=student, date=date, meal=slot).first()
    if intent:
        return describe_intent(student, date, slot, intent)
    return resolve_default_intent(student, date, slot)


@transaction.atomic
def set_intent(
    student,
    date: dt.date,
    slot: MealSlot,
    status: str,
    source: str = MealIntent.Source.ONE_TAP,
    arrival: str | None = None,
) -> tuple[MealIntent, bool]:
    if status not in MealIntent.Status.values:
        raise ValueError(f"Invalid intent status: {status}")
    if source not in MealIntent.Source.values:
        raise ValueError(f"Invalid intent source: {source}")
    arrival = arrival or _historical_arrival(student, date, slot)
    if arrival not in MealIntent.Arrival.values:
        raise ValueError(f"Invalid arrival window: {arrival}")
    intent, created = MealIntent.objects.update_or_create(
        student=student,
        date=date,
        meal=slot,
        defaults={
            "status": status,
            "source": source,
            "expected_arrival_window": arrival,
        },
    )
    if status == MealIntent.Status.NO:
        award_skip_notice(student, date, slot)
    return intent, created


def save_meal_feedback(
    student,
    date: dt.date,
    slot: MealSlot,
    response: str,
    suggestion: str = "",
) -> MealFeedback:
    """Upsert one student's answer about a date × meal menu."""
    response = (response or "").lower()
    if response not in MealFeedback.Response.values:
        raise ValueError("Choose yes or no.")
    suggestion = (suggestion or "").strip()[:240]
    feedback, _ = MealFeedback.objects.update_or_create(
        student=student,
        date=date,
        meal=slot,
        defaults={"response": response, "suggestion": suggestion},
    )
    return feedback


def feedback_counts(date: dt.date, slot: MealSlot) -> dict[str, int]:
    rows = (
        MealFeedback.objects.filter(date=date, meal=slot)
        .values("response")
        .annotate(n=Count("id"))
    )
    counts = {choice: 0 for choice in MealFeedback.Response.values}
    for row in rows:
        counts[row["response"]] = row["n"]
    counts["TOTAL"] = sum(counts[choice] for choice in MealFeedback.Response.values)
    return counts


def intent_counts(date: dt.date, slot: MealSlot) -> dict[str, int]:
    rows = (
        MealIntent.objects.filter(date=date, meal=slot)
        .values("status")
        .annotate(n=Count("id"))
    )
    counts = {status: 0 for status in MealIntent.Status.values}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["TOTAL"] = sum(counts[s] for s in MealIntent.Status.values)
    counts["POSITIVE"] = counts["YES"] + counts["MAYBE"]
    return counts


def declared_positive_intents(date: dt.date, slot: MealSlot) -> int:
    return MealIntent.objects.filter(
        date=date, meal=slot, status__in=[MealIntent.Status.YES, MealIntent.Status.MAYBE]
    ).count()


def dishes_for(date: dt.date, slot: MealSlot) -> list[Dish]:
    menu = menu_for(date, slot)
    if not menu:
        return []
    return [item.dish for item in menu.items.all()]


def diet_allows(profile: StudentProfile | None, dish: Dish) -> bool:
    diet = profile.diet if profile else StudentProfile.Diet.VEG
    if diet == StudentProfile.Diet.NONVEG:
        return True
    if diet == StudentProfile.Diet.EGG:
        return dish.diet_type in {Dish.DietType.VEG, Dish.DietType.JAIN, Dish.DietType.EGG}
    if diet == StudentProfile.Diet.JAIN:
        return dish.diet_type in {Dish.DietType.VEG, Dish.DietType.JAIN}
    return dish.diet_type in {Dish.DietType.VEG, Dish.DietType.JAIN}


@transaction.atomic
def check_in_student(
    student,
    date: dt.date,
    slot: MealSlot,
    dish_ids: list[int] | None = None,
    clean_plate: bool = True,
    source: str = CheckIn.Source.QR,
    timestamp: dt.datetime | None = None,
) -> CheckIn:
    if CheckIn.objects.filter(student=student, date=date, meal=slot).exists():
        raise ValueError("Already checked in for this meal.")
    if source not in CheckIn.Source.values:
        raise ValueError("Invalid check-in source.")
    allowed = {d.pk: d for d in dishes_for(date, slot)}
    chosen: list[Dish] = []
    for dish_id in dish_ids or []:
        if int(dish_id) not in allowed:
            raise ValueError(f"Dish {dish_id} is not on the menu for this meal.")
        chosen.append(allowed[int(dish_id)])
    start, end = meal_window(date, slot)
    ts = timestamp or timezone.now()
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.get_current_timezone())
    checkin = CheckIn.objects.create(
        student=student,
        date=date,
        meal=slot,
        timestamp=ts,
        clean_plate=clean_plate,
        source=source,
    )
    if chosen:
        CheckInDish.objects.bulk_create(
            [CheckInDish(check_in=checkin, dish=dish, portions=1) for dish in chosen]
        )
    award_checkin_rewards(checkin)
    return checkin


def meal_service_for(date: dt.date, slot: MealSlot) -> MealService | None:
    return MealService.objects.filter(date=date, meal=slot).first()


def checkin_count(date: dt.date, slot: MealSlot) -> int:
    return CheckIn.objects.filter(date=date, meal=slot).count()


def attendance_ratio(date: dt.date, slot: MealSlot) -> float | None:
    service = meal_service_for(date, slot)
    if not service or not service.actual_participants:
        return None
    return checkin_count(date, slot) / service.actual_participants


def service_level() -> float:
    return settings.MESSLAB["SERVICE_LEVEL"]


def round_kg(value: float) -> Decimal:
    """Kitchen-friendly quarter-kilo rounding."""
    return Decimal(str(max(0.0, round(float(value) * 4) / 4))).quantize(Decimal("0.01"))


def average_kg_per_participant(days: int = 30) -> float:
    qs = MealService.objects.filter(actual_participants__gt=0).order_by("-date")[: days * 4]
    from .models import WasteLog

    total_participants = sum(s.actual_participants for s in qs)
    if not total_participants:
        return settings.MESSLAB["AVG_KG_PER_STUDENT"]
    dates = [s.date for s in qs]
    meals = [s.meal_id for s in qs]
    total_consumed = 0.0
    for log in WasteLog.objects.filter(date__in=dates, meal_id__in=meals):
        total_consumed += float(log.consumed_kg)
    if total_consumed <= 0:
        return settings.MESSLAB["AVG_KG_PER_STUDENT"]
    return total_consumed / total_participants


def _as_kg(value, label: str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if amount < 0:
        raise ValueError(f"{label} cannot be negative.")
    return amount


def record_waste(
    date: dt.date,
    slot: MealSlot,
    dish: Dish,
    prepared_kg,
    consumed_kg,
    wasted_kg,
    *,
    leftover_reusable: bool = False,
    actor=None,
    ip_address: str | None = None,
) -> WasteLog:
    """Create or replace one dish-level waste record.

    The kitchen enters what it prepared, what was eaten and what was thrown
    away. Amounts are validated against each other so analytics never sees an
    impossible row.
    """
    prepared = _as_kg(prepared_kg, "Prepared kg")
    consumed = _as_kg(consumed_kg, "Consumed kg")
    wasted = _as_kg(wasted_kg, "Wasted kg")
    if wasted > prepared:
        raise ValueError("Wasted kg cannot exceed prepared kg.")
    if consumed > prepared:
        raise ValueError("Consumed kg cannot exceed prepared kg.")
    if consumed + wasted > prepared:
        raise ValueError("Consumed plus wasted kg cannot exceed prepared kg.")
    if date > today():
        raise ValueError("Waste can only be recorded for today or an earlier day.")

    with transaction.atomic():
        log, _ = WasteLog.objects.update_or_create(
            date=date,
            meal=slot,
            dish=dish,
            defaults={
                "prepared_kg": prepared,
                "consumed_kg": consumed,
                "wasted_kg": wasted,
                "leftover_reusable": bool(leftover_reusable),
            },
        )
        PreparationPlan.objects.filter(date=date, meal=slot, dish=dish).update(
            actual_prepared_kg=prepared
        )
        AccountAudit.objects.create(
            actor=actor,
            action=AccountAudit.Action.WASTE_SAVE,
            detail={
                "date": date.isoformat(),
                "meal": slot.name,
                "dish": dish.name,
                "prepared_kg": str(prepared),
                "consumed_kg": str(consumed),
                "wasted_kg": str(wasted),
            },
            ip_address=ip_address,
        )
    return log


def close_service_day(
    date: dt.date,
    *,
    actor=None,
    ip_address: str | None = None,
) -> dict:
    """Freeze a service day into ``MealService`` rows for model training.

    A day can only be closed today or earlier. Closing is idempotent: re-running
    it refreshes participant and intent counts without duplicating rows.
    """
    if date > today():
        raise ValueError("A future service day cannot be closed.")
    slots = list(MealSlot.objects.all())
    closed = []
    total_checkins = 0
    total_intents = 0
    with transaction.atomic():
        for slot in slots:
            has_menu = menu_for(date, slot) is not None
            checkins = CheckIn.objects.filter(date=date, meal=slot).count()
            intents = declared_positive_intents(date, slot)
            if not has_menu and not checkins and not intents:
                continue
            service, _ = MealService.objects.update_or_create(
                date=date,
                meal=slot,
                defaults={
                    "actual_participants": checkins,
                    "declared_intents": intents,
                },
            )
            closed.append(service)
            total_checkins += checkins
            total_intents += intents
        waste = (
            WasteLog.objects.filter(date=date).aggregate(total=Sum("wasted_kg"))["total"]
            or Decimal("0")
        )
        AccountAudit.objects.create(
            actor=actor,
            action=AccountAudit.Action.SERVICE_CLOSE,
            detail={
                "date": date.isoformat(),
                "meals": len(closed),
                "checkins": total_checkins,
                "positive_intents": total_intents,
                "wasted_kg": str(waste),
            },
            ip_address=ip_address,
        )
    return {
        "date": date,
        "meals": len(closed),
        "checkins": total_checkins,
        "positive_intents": total_intents,
        "wasted_kg": waste,
        "services": closed,
    }


def service_day_summary(date: dt.date) -> list[dict]:
    """What the manager needs on the close-day screen."""
    rows = []
    for slot in MealSlot.objects.all():
        service = meal_service_for(date, slot)
        rows.append(
            {
                "slot": slot,
                "menu": menu_for(date, slot),
                "checkins": CheckIn.objects.filter(date=date, meal=slot).count(),
                "positive_intents": declared_positive_intents(date, slot),
                "service": service,
                "waste_kg": WasteLog.objects.filter(date=date, meal=slot).aggregate(
                    total=Sum("wasted_kg")
                )["total"]
                or Decimal("0"),
            }
        )
    return rows
