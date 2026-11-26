"""Green points, streaks and badges. Deliberately lightweight and non-punitive."""
from __future__ import annotations

import datetime as dt

from django.db.models import Count, Q, Sum

from accounts.models import StudentProfile, User

from .models import Badge, RewardEvent

POINTS = {
    "checkin": 3,
    "intent_honoured": 10,
    "skip_notice": 8,
    "clean_plate": 6,
    "streak": 15,
    "badge": 0,
}


def _award(student, date: dt.date, reason: str, points: int, key: str, detail: str = "", badge=None):
    if RewardEvent.objects.filter(unique_key=key).exists():
        return None
    return RewardEvent.objects.create(
        student=student,
        date=date,
        reason=reason,
        points=points,
        unique_key=key,
        detail=detail,
        badge=badge,
    )


def award_checkin_rewards(checkin) -> list[RewardEvent]:
    events = []
    student = checkin.student
    date = checkin.date
    meal_key = checkin.meal.name
    base_key = f"{student.pk}:{date.isoformat()}:{meal_key}"
    events.append(
        _award(student, date, RewardEvent.Reason.CHECKIN, POINTS["checkin"], f"{base_key}:checkin",
               detail=f"Checked in for {meal_key}")
    )
    if checkin.clean_plate:
        events.append(
            _award(student, date, RewardEvent.Reason.CLEAN_PLATE, POINTS["clean_plate"],
                   f"{base_key}:clean", detail="Clean plate")
        )
    from dining.models import MealIntent

    intent = MealIntent.objects.filter(student=student, date=date, meal=checkin.meal).first()
    if intent and intent.status in {MealIntent.Status.YES, MealIntent.Status.MAYBE}:
        events.append(
            _award(student, date, RewardEvent.Reason.INTENT_HONOURED, POINTS["intent_honoured"],
                   f"{base_key}:honoured", detail="Intent matched attendance")
        )
    events = [e for e in events if e is not None]
    check_streak(student, date)
    return events


def award_skip_notice(student, date: dt.date, slot) -> RewardEvent | None:
    """Only award a skip notice if it is declared before the meal starts."""
    from django.utils import timezone

    from dining.services import meal_window

    start, _end = meal_window(date, slot)
    if timezone.now() >= start + dt.timedelta(minutes=20):
        return None
    key = f"{student.pk}:{date.isoformat()}:{slot.name}:skip_notice"
    return _award(
        student,
        date,
        RewardEvent.Reason.SKIP_NOTICE,
        POINTS["skip_notice"],
        key,
        detail=f"Told the mess early about skipping {slot.name}",
    )


def check_streak(student, date: dt.date) -> int:
    """Count consecutive days with a check-in or a notified skip, ending on date."""
    from dining.models import CheckIn, MealIntent

    days = set(
        CheckIn.objects.filter(student=student, date__lte=date)
        .order_by("-date")
        .values_list("date", flat=True)[:60]
    )
    days.update(
        MealIntent.objects.filter(
            student=student, date__lte=date, status=MealIntent.Status.NO
        )
        .order_by("-date")
        .values_list("date", flat=True)[:60]
    )
    streak = 0
    cursor = date
    while cursor in days:
        streak += 1
        cursor -= dt.timedelta(days=1)
    if streak >= 3:
        badge = Badge.objects.filter(code="streak_3").first()
        key = f"{student.pk}:{date.isoformat()}:streak_badge"
        _award(
            student,
            date,
            RewardEvent.Reason.BADGE,
            0,
            key,
            detail=f"{streak}-day green streak",
            badge=badge,
        )
    if streak >= 7:
        badge = Badge.objects.filter(code="streak_7").first()
        key = f"{student.pk}:{date.isoformat()}:streak7_badge"
        _award(
            student,
            date,
            RewardEvent.Reason.BADGE,
            0,
            key,
            detail="7-day green streak",
            badge=badge,
        )
    return streak


def current_streak(student, as_of: dt.date | None = None) -> int:
    from django.utils import timezone

    as_of = as_of or timezone.localdate()
    return check_streak(student, as_of)


def student_summary(student, as_of: dt.date | None = None) -> dict:
    from django.utils import timezone

    as_of = as_of or timezone.localdate()
    totals = RewardEvent.objects.filter(student=student).aggregate(
        points=Sum("points"),
        clean=Count("id", filter=Q(reason=RewardEvent.Reason.CLEAN_PLATE)),
        honoured=Count("id", filter=Q(reason=RewardEvent.Reason.INTENT_HONOURED)),
        skips=Count("id", filter=Q(reason=RewardEvent.Reason.SKIP_NOTICE)),
    )
    badges = list(
        Badge.objects.filter(
            rewardevent__student=student, rewardevent__reason=RewardEvent.Reason.BADGE
        ).distinct()
    )
    points = totals["points"] or 0
    profile = getattr(student, "profile", None)
    hostel_rank = None
    if profile and profile.hostel and student.effective_role == User.Role.HOSTEL_STUDENT:
        hostel_points = (
            RewardEvent.objects.filter(
                student__role_profile__role=User.Role.HOSTEL_STUDENT,
                student__profile__hostel=profile.hostel,
            )
            .values("student")
            .annotate(total=Sum("points"))
            .order_by("-total")
        )
        for idx, row in enumerate(hostel_points, start=1):
            if row["student"] == student.pk:
                hostel_rank = idx
                break
    next_badge = (
        Badge.objects.filter(points__gt=points).order_by("points").first()
        if points
        else Badge.objects.order_by("points").first()
    )
    return {
        "points": points,
        "clean_plates": totals["clean"] or 0,
        "honoured": totals["honoured"] or 0,
        "skips": totals["skips"] or 0,
        "streak": current_streak(student, as_of),
        "badges": badges,
        "hostel_rank": hostel_rank,
        "next_badge": next_badge,
    }


def award_day_rewards(date: dt.date) -> int:
    """Bulk, idempotent rewards for one service day."""
    from dining.models import CheckIn, MealIntent, MealSlot

    checkins = list(
        CheckIn.objects.filter(date=date).values("student_id", "meal_id", "clean_plate")
    )
    intents = {
        (i.student_id, i.meal_id): i.status
        for i in MealIntent.objects.filter(date=date).only("student_id", "meal_id", "status")
    }
    meal_names = {s.pk: s.name for s in MealSlot.objects.all()}
    events = []
    for c in checkins:
        sid = c["student_id"]
        key = f"{sid}:{date.isoformat()}:{meal_names[c['meal_id']]}"
        events.append(
            RewardEvent(
                student_id=sid,
                date=date,
                reason=RewardEvent.Reason.CHECKIN,
                points=POINTS["checkin"],
                unique_key=f"{key}:checkin",
                detail="Meal check-in",
            )
        )
        if c["clean_plate"]:
            events.append(
                RewardEvent(
                    student_id=sid,
                    date=date,
                    reason=RewardEvent.Reason.CLEAN_PLATE,
                    points=POINTS["clean_plate"],
                    unique_key=f"{key}:clean",
                    detail="Clean plate",
                )
            )
        if intents.get((sid, c["meal_id"])) in {"YES", "MAYBE"}:
            events.append(
                RewardEvent(
                    student_id=sid,
                    date=date,
                    reason=RewardEvent.Reason.INTENT_HONOURED,
                    points=POINTS["intent_honoured"],
                    unique_key=f"{key}:honoured",
                    detail="Intent matched attendance",
                )
            )
    for (sid, meal_id), status in intents.items():
        if status != "NO":
            continue
        events.append(
            RewardEvent(
                student_id=sid,
                date=date,
                reason=RewardEvent.Reason.SKIP_NOTICE,
                points=POINTS["skip_notice"],
                unique_key=f"{sid}:{date.isoformat()}:{meal_names[meal_id]}:skip_notice",
                detail=f"Told the mess early about skipping {meal_names[meal_id]}",
            )
        )
    # One badge check per student with a check-in today.
    from django.db.models import Count

    active = {c["student_id"] for c in checkins}
    if active:
        clean_counts = {
            row["student_id"]: row["n"]
            for row in RewardEvent.objects.filter(
                student_id__in=active, reason=RewardEvent.Reason.CLEAN_PLATE
            )
            .values("student_id")
            .annotate(n=Count("id"))
        }
        skip_counts = {
            row["student_id"]: row["n"]
            for row in RewardEvent.objects.filter(
                student_id__in=active, reason=RewardEvent.Reason.SKIP_NOTICE
            )
            .values("student_id")
            .annotate(n=Count("id"))
        }
        for sid in active:
            if clean_counts.get(sid, 0) == 10:
                badge = Badge.objects.filter(code="clean_10").first()
                if badge:
                    events.append(
                        RewardEvent(
                            student_id=sid,
                            date=date,
                            reason=RewardEvent.Reason.BADGE,
                            points=badge.points,
                            badge=badge,
                            unique_key=f"{sid}:{date.isoformat()}:badge:clean_10",
                            detail=badge.name,
                        )
                    )
            if skip_counts.get(sid, 0) == 10:
                badge = Badge.objects.filter(code="zero_waste").first()
                if badge:
                    events.append(
                        RewardEvent(
                            student_id=sid,
                            date=date,
                            reason=RewardEvent.Reason.BADGE,
                            points=badge.points,
                            badge=badge,
                            unique_key=f"{sid}:{date.isoformat()}:badge:zero_waste",
                            detail=badge.name,
                        )
                    )
    if events:
        RewardEvent.objects.bulk_create(events, ignore_conflicts=True, batch_size=3000)
    return len(events)


def hostel_leaderboard(as_of: dt.date | None = None, days: int = 30) -> list[dict]:
    from django.utils import timezone

    from dining.models import MealService, WasteLog

    as_of = as_of or timezone.localdate()
    since = as_of - dt.timedelta(days=days)
    points = (
        RewardEvent.objects.filter(
            date__gte=since,
            date__lte=as_of,
            student__role_profile__role=User.Role.HOSTEL_STUDENT,
            student__profile__hostel__isnull=False,
        )
        .values("student__profile__hostel")
        .annotate(points=Sum("points"), events=Count("id"))
        .order_by("-points")
    )
    skips = (
        RewardEvent.objects.filter(
            date__gte=since,
            date__lte=as_of,
            reason=RewardEvent.Reason.SKIP_NOTICE,
            student__role_profile__role=User.Role.HOSTEL_STUDENT,
        )
        .values("student__profile__hostel")
        .annotate(n=Count("id"))
    )
    skip_map = {row["student__profile__hostel"]: row["n"] for row in skips}
    services = list(
        MealService.objects.filter(date__gte=since, date__lte=as_of, actual_participants__gt=0)
    )
    participant_total = sum(s.actual_participants for s in services)
    consumed_total = sum(
        float(w.consumed_kg)
        for w in WasteLog.objects.filter(date__gte=since, date__lte=as_of)
    )
    avg_kg = (consumed_total / participant_total) if participant_total else 0.35
    rows = []
    for row in points:
        hostel = row["student__profile__hostel"]
        if not hostel:
            continue
        skip_count = skip_map.get(hostel, 0)
        rows.append(
            {
                "hostel": hostel,
                "label": dict(StudentProfile.Hostel.choices).get(hostel, hostel),
                "points": row["points"] or 0,
                "events": row["events"],
                "skip_notices": skip_count,
                "estimated_kg_saved": round(skip_count * avg_kg, 1),
            }
        )
    return rows
