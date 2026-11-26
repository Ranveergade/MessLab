"""Small, explicit factories so tests never depend on seeded sample data."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from accounts.models import StudentProfile, User
from dining.models import (
    CheckIn,
    DayContext,
    Forecast,
    MealIntent,
    MealService,
    PreparationPlan,
    WasteLog,
)
from menu.models import DailyMenu, Dish, MealSlot, MenuItem

BASE_DATE = dt.date(2026, 3, 2)  # a Monday


def make_slots() -> dict[str, MealSlot]:
    specs = {
        "breakfast": (dt.time(7, 30), dt.time(9, 30), dt.time(8, 15), dt.time(8, 45), 3, 1),
        "lunch": (dt.time(12, 0), dt.time(14, 30), dt.time(13, 0), dt.time(13, 30), 4, 2),
        "snacks": (dt.time(16, 30), dt.time(18, 0), dt.time(17, 0), dt.time(17, 20), 2, 3),
        "dinner": (dt.time(19, 30), dt.time(21, 30), dt.time(20, 0), dt.time(20, 30), 4, 4),
    }
    slots = {}
    for name, (start, end, peak_start, peak_end, counters, order) in specs.items():
        slots[name] = MealSlot.objects.create(
            name=name,
            start_time=start,
            end_time=end,
            peak_start=peak_start,
            peak_end=peak_end,
            counters=counters,
            counter_capacity_per_hour=420,
            order=order,
        )
    return slots


def make_user(
    username: str = "user1",
    role: str = User.Role.STUDENT,
    *,
    password: str = "test-pass-123",
    hostel: str | None = None,
    email: str | None = None,
) -> User:
    """Create a user with a matching role profile and the right optional profile."""
    user = User.objects.create_user(
        username=username,
        email=email or f"{username}@messlab.test",
        password=password,
        role=role,
    )
    profile = user.role_profile
    profile.role = role
    profile.hostel = hostel if role == User.Role.HOSTEL_STUDENT else None
    profile.save()
    return user


def make_student(
    username: str = "student1",
    *,
    password: str = "test-pass-123",
    role: str = User.Role.HOSTEL_STUDENT,
    hostel: str | None = StudentProfile.Hostel.H1,
    diet: str = StudentProfile.Diet.VEG,
    default_plan: dict | None = None,
    email: str | None = None,
) -> User:
    user = make_user(username, role, password=password, hostel=hostel, email=email)
    profile = user.profile
    profile.hostel = hostel if role == User.Role.HOSTEL_STUDENT else None
    profile.diet = diet
    if default_plan is not None:
        profile.default_plan = default_plan
    profile.save()
    return user


def make_manager(
    username: str = "manager1",
    *,
    password: str = "test-pass-123",
    email: str | None = None,
) -> User:
    return make_user(username, User.Role.MANAGER, password=password, email=email)


def make_dish(
    name: str,
    *,
    category: str = Dish.Category.MAIN,
    diet_type: str = Dish.DietType.VEG,
    portion: int = 180,
    popularity: float = 0.5,
) -> Dish:
    return Dish.objects.create(
        name=name,
        category=category,
        diet_type=diet_type,
        avg_portion_grams=portion,
        popularity_baseline=popularity,
    )


def make_menu(
    date: dt.date,
    slot: MealSlot,
    dishes: list[tuple[Dish, float]],
    *,
    special: bool = False,
) -> DailyMenu:
    menu = DailyMenu.objects.create(date=date, meal=slot, is_special=special)
    for dish, kg in dishes:
        MenuItem.objects.create(
            daily_menu=menu, dish=dish, planned_quantity_kg=Decimal(str(kg))
        )
    return menu


def make_service(
    date: dt.date,
    slot: MealSlot,
    participants: int,
    *,
    declared: int = 0,
    weather: str = "clear",
    holiday: bool = False,
    exam: bool = False,
) -> MealService:
    return MealService.objects.create(
        date=date,
        meal=slot,
        actual_participants=participants,
        declared_intents=declared,
        weather=weather,
        is_holiday=holiday,
        is_exam=exam,
    )


def make_waste(
    date: dt.date,
    slot: MealSlot,
    dish: Dish,
    prepared: float,
    consumed: float,
    wasted: float,
) -> WasteLog:
    return WasteLog.objects.create(
        date=date,
        meal=slot,
        dish=dish,
        prepared_kg=Decimal(str(prepared)),
        consumed_kg=Decimal(str(consumed)),
        wasted_kg=Decimal(str(wasted)),
    )


def make_plan(
    date: dt.date,
    slot: MealSlot,
    dish: Dish,
    recommended: float,
    baseline: float,
    *,
    status: str = PreparationPlan.Status.DRAFT,
) -> PreparationPlan:
    return PreparationPlan.objects.create(
        date=date,
        meal=slot,
        dish=dish,
        recommended_kg=Decimal(str(recommended)),
        baseline_kg=Decimal(str(baseline)),
        status=status,
    )


def make_forecast(
    date: dt.date,
    slot: MealSlot,
    *,
    dish: Dish | None = None,
    participation: int = 100,
    demand: float = 10.0,
    lower: float | None = None,
    upper: float | None = None,
) -> Forecast:
    lower = demand * 0.9 if lower is None else lower
    upper = demand * 1.1 if upper is None else upper
    return Forecast.objects.create(
        date=date,
        meal=slot,
        dish=dish,
        predicted_participation=participation,
        predicted_demand_kg=Decimal(str(demand)),
        lower_bound_kg=Decimal(str(lower)),
        upper_bound_kg=Decimal(str(upper)),
        lower_participation=max(0, int(participation * 0.9)),
        upper_participation=int(participation * 1.1),
        model_version="test",
    )


def make_intent(
    student: User,
    date: dt.date,
    slot: MealSlot,
    status: str = MealIntent.Status.YES,
) -> MealIntent:
    return MealIntent.objects.create(student=student, date=date, meal=slot, status=status)


def make_checkin(
    student: User,
    date: dt.date,
    slot: MealSlot,
    *,
    clean_plate: bool = True,
) -> CheckIn:
    return CheckIn.objects.create(
        student=student,
        date=date,
        meal=slot,
        clean_plate=clean_plate,
        source=CheckIn.Source.QR,
    )


def set_today(date: dt.date | None = None) -> None:
    """Pin the service date for tests; pass ``None`` to use the real date."""
    import dining.services as services

    services._TEST_TODAY = date


def make_context(date: dt.date, **kwargs) -> DayContext:
    return DayContext.objects.create(date=date, **kwargs)


def latest_otp_code(index: int = -1) -> str:
    """Read a one-time code from the test mail outbox.

    Production shows the code in the server terminal, never in an HTTP
    response, so tests read the email instead of a response field.
    """
    import re

    from django.core import mail

    if not mail.outbox:
        raise AssertionError("No OTP email was sent.")
    body = mail.outbox[index].body
    match = re.search(r"\b(\d{6})\b", body)
    if not match:
        raise AssertionError("No six-digit code found in the OTP email.")
    return match.group(1)


def otp_emails_sent() -> int:
    from django.core import mail

    return len(mail.outbox)
