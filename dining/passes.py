"""Signed check-in passes, QR rendering and redemption.

A pass is a compact signed string, not a database row: it names the student, the
service date and the account's ``token_version``. Rotating the version (role
change, deactivation) invalidates every outstanding pass immediately. The QR
itself carries no name or student code, so a screenshot is useless without the
server's signature.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core import signing
from django.utils import timezone

from accounts.models import StudentProfile, User

from .models import CheckIn, MealSlot
from .services import check_in_student, meal_window, today

PASS_SALT = "messlab.checkin.pass"
# A pass is issued for one meal and stays readable for seconds, not hours.
# ``MESSLAB["QR_TTL_SECONDS"]`` is the single source of truth.
PASS_MAX_AGE = 12 * 60 * 60
EARLY_GRACE = dt.timedelta(minutes=30)


class PassError(Exception):
    """A pass or student code that cannot be redeemed."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def pass_meal(date: dt.date, *, now: dt.datetime | None = None) -> MealSlot | None:
    """The meal a fresh pass should be bound to.

    Prefer the meal being served; outside service hours bind to the first meal
    so the page still renders. The pass is only redeemable inside its own meal
    window, so a between-meals pass cannot be used early.
    """
    return open_meal(date, now=now) or MealSlot.objects.order_by(
        "order", "start_time"
    ).first()


def issue_pass(
    user: User,
    date: dt.date | None = None,
    meal: MealSlot | None = None,
) -> str:
    date = date or today()
    meal = meal or pass_meal(date)
    if meal is None:
        raise PassError("Add a meal slot before issuing passes.", "no_meal")
    return signing.dumps(
        {
            "u": user.pk,
            "d": date.isoformat(),
            "v": user.token_version,
            "m": meal.name,
        },
        salt=PASS_SALT,
        compress=True,
    )


def read_pass(
    token: str,
    *,
    date: dt.date | None = None,
    meal: MealSlot | None = None,
) -> tuple[User, dt.date]:
    """Verify the signature and return the student and the pass date."""
    token = (token or "").strip()
    if not token:
        raise PassError("Scan a pass or type a student code.", "empty")
    if len(token) > 2048:
        raise PassError("That is not a valid MessLab pass.", "invalid")
    max_age = int(settings.MESSLAB.get("QR_TTL_SECONDS", 90))
    try:
        payload = signing.loads(token, salt=PASS_SALT, max_age=max_age)
    except signing.SignatureExpired:
        raise PassError("This pass has expired. Ask the student to refresh it.", "expired")
    except signing.BadSignature:
        raise PassError("That is not a valid MessLab pass.", "invalid")

    try:
        user_id = int(payload["u"])
        pass_date = dt.date.fromisoformat(payload["d"])
        version = int(payload["v"])
        meal_name = payload["m"]
    except (KeyError, TypeError, ValueError):
        raise PassError("That is not a valid MessLab pass.", "invalid")

    user = User.objects.filter(pk=user_id).first()
    if user is None or not user.is_active:
        raise PassError("This account cannot check in.", "inactive")
    if user.token_version != version:
        raise PassError("This pass is no longer valid. Ask the student to refresh it.", "stale")

    service_date = date or today()
    if pass_date != service_date:
        raise PassError("This pass is for another service day.", "wrong_day")
    if meal is not None and meal.name != meal_name:
        raise PassError("This pass is for another meal. Ask the student to refresh it.", "wrong_meal")
    return user, pass_date


def open_meal(date: dt.date, *, now: dt.datetime | None = None) -> MealSlot | None:
    """The meal currently being served, allowing a little counter grace time."""
    now = now or timezone.now()
    for slot in MealSlot.objects.all():
        start, end = meal_window(date, slot)
        if start - EARLY_GRACE <= now <= end + EARLY_GRACE:
            return slot
    return None


def _redeem(user: User, date: dt.date, slot: MealSlot) -> CheckIn:
    try:
        return check_in_student(user, date, slot, source=CheckIn.Source.QR)
    except ValueError as exc:
        raise PassError(str(exc), "duplicate")


def redeem_pass(
    token: str,
    *,
    meal: MealSlot | None = None,
    date: dt.date | None = None,
) -> tuple[User, MealSlot, CheckIn]:
    """Validate a scanned pass and record the check-in."""
    service_date = date or today()
    slot = meal or open_meal(service_date)
    if slot is None:
        raise PassError("No meal is being served right now.", "closed")
    user, pass_date = read_pass(token, date=service_date, meal=slot)
    return user, slot, _redeem(user, pass_date, slot)


def student_by_code(code: str) -> User:
    """Resolve a typed student code to an active account."""
    code = (code or "").strip().upper()
    if not code:
        raise PassError("Type the student code from the ID card.", "empty")
    profile = (
        StudentProfile.objects.select_related("user")
        .filter(student_code__iexact=code)
        .first()
    )
    if profile is None or not profile.user.is_active:
        raise PassError("No active student matches that code.", "unknown")
    return profile.user


def manual_checkin(
    code: str,
    *,
    meal: MealSlot | None = None,
    date: dt.date | None = None,
) -> tuple[User, MealSlot, CheckIn]:
    """Check in a student by typed code, for counters without a camera."""
    user = student_by_code(code)
    service_date = date or today()
    slot = meal or open_meal(service_date)
    if slot is None:
        raise PassError("No meal is being served right now.", "closed")
    try:
        checkin = check_in_student(user, service_date, slot, source=CheckIn.Source.MANUAL)
    except ValueError as exc:
        raise PassError(str(exc), "duplicate")
    return user, slot, checkin


def pass_svg(token: str, *, scale: int = 4) -> str:
    """Inline SVG for the pass. segno is pure Python and works offline."""
    import segno

    qr = segno.make(token, error="m")
    return qr.svg_inline(scale=scale, dark="#123f2f", light="#ffffff", border=2)
