"""Server-rendered student and manager interfaces."""
from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as session_login, logout as session_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.account_admin import (
    AccountAdminError,
    can_manage_target,
    change_role,
    set_active,
    target_code,
)
from accounts.forms import RegistrationForm, RegistrationVerifyForm
from accounts.models import (
    AccountAudit,
    StaffFeedback,
    StudentProfile,
    User,
    WEEKDAYS,
    validate_default_plan,
)
from accounts.otp import mask_email, request_login_otp, verify_otp
from accounts.permissions import perm_required
from accounts.rbac import landing_for, role_of, user_can
from accounts.registration import complete_registration, start_registration
from dining.importers import MAX_CSV_BYTES, CsvImportError, import_csv_file
from dining.forms import WasteEntryForm
from dining.models import (
    CheckIn,
    DataImport,
    Forecast,
    MealFeedback,
    MealIntent,
    MealSlot,
    PlanDecisionLog,
    PreparationPlan,
    WasteLog,
)
from dining.passes import (
    PassError,
    issue_pass,
    manual_checkin,
    open_meal,
    pass_meal,
    pass_svg,
    redeem_pass,
)
from dining.services import (
    close_service_day,
    describe_intent,
    feedback_counts,
    meal_window,
    record_waste,
    resolve_default_intent,
    save_meal_feedback,
    service_day_summary,
    set_intent,
    today,
)
from menu.forms import DishForm, MealSlotForm, MenuMetaForm
from menu.models import DailyMenu, Dish
from menu.services import MenuError, save_daily_menu, save_dish, toggle_dish
from rewards.models import RewardEvent
from rewards.services import hostel_leaderboard, student_summary

from .privacy import delete_person, export_person
from .services import (
    assumptions,
    crowd_forecast,
    dashboard_context,
    impact_summary,
    live_meal_status,
    menu_insights,
    model_health,
    peak_analysis,
    today_meals,
    waste_analytics,
)


def _next_url(request) -> str:
    """Only ever bounce back to a URL on this host."""
    raw = request.POST.get("next") or request.GET.get("next") or ""
    if raw and url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return raw
    return ""


def _login_context(request, **extra) -> dict:
    return {
        "allow_password_login": settings.MESSLAB["ALLOW_PASSWORD_LOGIN"],
        "next": _next_url(request),
        **extra,
    }


def otp_login(request):
    """Two-step email login: request a code, then verify it.

    The request step always answers the same way, so it cannot be used to probe
    which email addresses have accounts.
    """
    if request.user.is_authenticated:
        return redirect(landing_for(request.user))

    step = "email"
    email = (request.POST.get("email") or request.GET.get("email") or "").strip().lower()
    error = ""

    if request.method == "POST":
        step = request.POST.get("step", "request")
        if step == "request":
            if "@" not in email or len(email) > 254:
                error = "Enter the email address registered with the mess."
                step = "email"
            else:
                request_login_otp(email, request=request)
                step = "verify"
        elif step == "verify":
            code = (request.POST.get("code") or "").strip()
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            result = verify_otp(user, code, request=request)
            if result.status == "ok" and result.user:
                session_login(request, result.user)
                return redirect(_next_url(request) or landing_for(result.user))
            error = {
                "expired": "That code has expired. Request a new one.",
                "lockout": "Too many attempts. Request a new code.",
            }.get(result.status, "That code is not right. Check your email and try again.")
            step = "verify"
        else:
            step = "email"
            error = "Enter your email address to continue."

    return render(
        request,
        "login.html",
        _login_context(
            request,
            step=step,
            email=email,
            error=error,
            otp_ttl_minutes=max(1, settings.OTP_TTL_SECONDS // 60),
        ),
    )


def register(request):
    """Two-step self-registration for student roles."""
    if request.user.is_authenticated:
        return redirect(landing_for(request.user))
    if not settings.MESSLAB.get("ALLOW_SELF_REGISTRATION", True):
        messages.error(request, "Self-registration is closed. Ask the mess office for an account.")
        return redirect("web:login")

    step = "email"
    email = (request.POST.get("email") or "").strip().lower()
    error = ""
    form = RegistrationForm(
        initial={
            "email": email,
            "role": User.Role.HOSTEL_STUDENT,
            "diet": StudentProfile.Diet.VEG,
        }
    )
    reg_data: dict = {}

    if request.method == "POST":
        step = request.POST.get("step", "request")
        if step == "request":
            form = RegistrationForm(request.POST)
            if form.is_valid():
                start_registration(**form.cleaned_data, request=request)
                reg_data = {
                    key: ("" if value is None else value)
                    for key, value in form.cleaned_data.items()
                }
                step = "verify"
            else:
                step = "email"
        elif step == "verify":
            verify_form = RegistrationVerifyForm(request.POST)
            reg_data = {
                key: request.POST.get(key, "")
                for key in (
                    "full_name",
                    "role",
                    "hostel",
                    "diet",
                    "phone",
                )
            }
            if verify_form.is_valid():
                result = complete_registration(
                    verify_form.cleaned_data["email"],
                    verify_form.cleaned_data["code"],
                    request=request,
                )
                if result.status == "ok" and result.user:
                    session_login(request, result.user)
                    messages.success(
                        request, "Account verified. Welcome to MessLab."
                    )
                    return redirect(landing_for(result.user))
                error = {
                    "expired": "That code has expired. Send a new one.",
                    "lockout": "Too many attempts. Send a new code.",
                }.get(
                    result.status,
                    "That code is not right. Check your email and try again.",
                )
            else:
                error = "Enter the 6-digit code from your email."
            step = "verify"
        else:
            step = "email"
            error = "Fill in the form to create your account."

    return render(
        request,
        "register.html",
        {
            "step": step,
            "email": email,
            "error": error,
            "form": form,
            "reg_data": reg_data,
            "otp_ttl_minutes": max(1, settings.OTP_TTL_SECONDS // 60),
            "next": _next_url(request),
        },
    )


class MessLabLoginView(LoginView):
    """Legacy password login, kept only for local recovery and Django admin."""

    template_name = "login_password.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if not settings.MESSLAB["ALLOW_PASSWORD_LOGIN"]:
            messages.info(request, "Password sign-in is disabled. Use your email code.")
            return redirect("web:login")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_login_context(self.request))
        return context


class MessLabLogoutView(LogoutView):
    next_page = "/login/"


def _date(request, key: str = "date") -> dt.date:
    raw = request.GET.get(key)
    if not raw:
        return today()
    return parse_date(raw) or today()


def _meal(request) -> MealSlot:
    name = request.GET.get("meal")
    slot = MealSlot.objects.filter(name=name).first() if name else None
    return slot or MealSlot.objects.filter(name="lunch").first() or MealSlot.objects.first()


@login_required
def home(request):
    return redirect(landing_for(request.user))


@perm_required("intent.write")
def student_home(request):
    date = today()
    cards = today_meals(request.user, date)
    rewards = student_summary(request.user, date)
    return render(
        request,
        "student/home.html",
        {
            "date": date,
            "cards": cards,
            "rewards": rewards,
            "leaderboard": hostel_leaderboard(date),
            "assumptions": assumptions(),
            "weekend_form_url": settings.MESSLAB.get("WEEKEND_WASTE_FORM_URL", ""),
            "active": "home",
        },
    )


@perm_required("plan.weekly")
def student_plan(request):
    profile = getattr(request.user, "profile", None)
    if not profile:
        messages.error(request, "Student profile is missing.")
        return redirect("web:student-home")
    if request.method == "POST":
        plan = {}
        for day, _label in WEEKDAYS:
            plan[str(day)] = {}
            for meal in ("breakfast", "lunch", "snacks", "dinner"):
                value = request.POST.get(f"plan_{day}_{meal}", "auto")
                if value not in {"eat", "skip", "auto"}:
                    value = "auto"
                plan[str(day)][meal] = value
        try:
            validate_default_plan(plan)
        except Exception as exc:
            messages.error(request, f"Could not save plan: {exc}")
        else:
            profile.default_plan = plan
            profile.save(update_fields=["default_plan", "updated_at"])
            messages.success(request, "Weekly plan saved. Tomorrow's defaults are already updated.")
            return redirect("web:student-plan")
    return render(
        request,
        "student/plan.html",
        {
            "profile": profile,
            "plan": profile.normalized_plan(),
            "weekdays": WEEKDAYS,
            "meals": ("breakfast", "lunch", "snacks", "dinner"),
            "today_index": today().weekday(),
            "active": "plan",
        },
    )


@require_POST
@perm_required("intent.write")
def student_intent(request):
    date = parse_date(request.POST.get("date", "")) or today()
    slot = get_object_or_404(MealSlot, name=request.POST.get("meal", ""))
    status = request.POST.get("status", "YES").upper()
    if status not in {"YES", "NO", "MAYBE"}:
        messages.error(request, "Invalid intent.")
        return redirect(request.POST.get("next") or reverse("web:student-home"))
    try:
        set_intent(request.user, date, slot, status, source="one_tap")
        messages.success(request, f"{slot.get_name_display()} updated: {status.title()}.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(request.POST.get("next") or reverse("web:student-home"))


@require_POST
@perm_required("intent.write")
def student_feedback(request):
    """Let a student say whether today's menu should be cooked, and suggest a dish."""
    date = parse_date(request.POST.get("date", "")) or today()
    if abs((date - today()).days) > 1:
        messages.error(request, "Feedback is only open for today's menu.")
        return redirect("web:student-home")
    slot = get_object_or_404(MealSlot, name=request.POST.get("meal", ""))
    try:
        save_meal_feedback(
            request.user,
            date,
            slot,
            request.POST.get("response", ""),
            request.POST.get("suggestion", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Thanks — your menu feedback for {slot.get_name_display()} is saved."
        )
    return redirect(request.POST.get("next") or reverse("web:student-home"))


@login_required
def privacy(request):
    return render(request, "privacy.html", {"active": "privacy"})


@login_required
def privacy_export(request):
    """Download everything tied to the signed-in account as JSON."""
    payload = export_person(request.user)
    response = JsonResponse(payload, json_dumps_params={"indent": 2})
    response["Content-Disposition"] = 'attachment; filename="messlab-my-data.json"'
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@login_required
def privacy_delete(request):
    """Delete the signed-in account after an explicit confirmation."""
    if (request.POST.get("confirm") or "").strip().upper() != "DELETE":
        messages.error(request, "Type DELETE to confirm account deletion.")
        return redirect("web:privacy")
    user = request.user
    session_logout(request)
    delete_person(user)
    messages.success(request, "Your account and personal records have been deleted.")
    return redirect("web:login")


@perm_required("qr.show")
def student_checkin(request):
    date = today()
    slots = list(MealSlot.objects.all())
    menus = {
        menu.meal_id: menu
        for menu in DailyMenu.objects.filter(date=date).prefetch_related("items__dish")
    }
    intents = {
        intent.meal_id: intent
        for intent in MealIntent.objects.filter(student=request.user, date=date)
    }
    checkins = {
        checkin.meal_id: checkin
        for checkin in request.user.check_ins.filter(date=date)
    }
    rows = []
    for slot in slots:
        menu = menus.get(slot.pk)
        intent = intents.get(slot.pk)
        rows.append(
            {
                "slot": slot,
                "menu": menu,
                "items": list(menu.items.all()) if menu else [],
                "checkin": checkins.get(slot.pk),
                "intent": (
                    describe_intent(request.user, date, slot, intent)
                    if intent
                    else resolve_default_intent(request.user, date, slot)
                ),
            }
        )
    return render(
        request,
        "student/checkin.html",
        {
            "date": date,
            "slots": rows,
            "pass_token": issue_pass(request.user, date),
            "pass_meal": pass_meal(date),
            "open_slot": open_meal(date),
            "pass_seconds": int(settings.MESSLAB.get("QR_TTL_SECONDS", 90)),
            "active": "checkin",
        },
    )


@perm_required("qr.show")
def student_pass_qr(request):
    """The signed pass as an SVG. Never cached: it carries a live signature."""
    token = issue_pass(request.user, today())
    response = HttpResponse(pass_svg(token), content_type="image/svg+xml")
    response["Cache-Control"] = "no-store, max-age=0"
    response["Pragma"] = "no-cache"
    return response


@perm_required("rewards.own")
def student_rewards(request):
    date = today()
    summary = student_summary(request.user, date)
    recent = (
        RewardEvent.objects.filter(student=request.user)
        .select_related("badge")
        .order_by("-created_at")[:30]
    )
    return render(
        request,
        "student/rewards.html",
        {
            "date": date,
            "summary": summary,
            "recent": recent,
            "leaderboard": hostel_leaderboard(date),
            "active": "rewards",
        },
    )


def _prep_sheet(date: dt.date) -> list[dict]:
    """Today's approved prep quantities. No forecast, no cost, no personal data."""
    rows = (
        PreparationPlan.objects.filter(
            date=date,
            status__in=[PreparationPlan.Status.APPROVED, PreparationPlan.Status.OVERRIDDEN],
        )
        .select_related("meal", "dish")
        .order_by("meal__order", "dish__name")
    )
    grouped: dict[int, dict] = {}
    for plan in rows:
        bucket = grouped.setdefault(
            plan.meal_id,
            {"meal": plan.meal, "dishes": []},
        )
        bucket["dishes"].append(plan)
    return list(grouped.values())


def _service_windows(date: dt.date) -> list[dict]:
    """Meal windows for today with their open/closed state."""
    now = timezone.now()
    windows = []
    for slot in MealSlot.objects.all():
        start, end = meal_window(date, slot)
        if now < start:
            state = "upcoming"
        elif now <= end:
            state = "open"
        else:
            state = "closed"
        windows.append({"slot": slot, "start": start, "end": end, "state": state})
    return windows


@perm_required("prep.view")
def staff_home(request):
    date = today()
    if request.method == "POST":
        kind = request.POST.get("kind", StaffFeedback.Kind.OPINION)
        message = (request.POST.get("message") or "").strip()
        if kind not in StaffFeedback.Kind.values:
            messages.error(request, "Choose opinion, need or issue.")
        elif not message:
            messages.error(request, "Write a short message first.")
        else:
            StaffFeedback.objects.create(author=request.user, kind=kind, message=message[:2000])
            messages.success(request, "Thank you. The supervisor will see this.")
            return redirect("web:staff-home")

    prep = _prep_sheet(date)
    total_kg = sum(
        float(plan.recommended_kg)
        for meal in prep
        for plan in meal["dishes"]
    )
    return render(
        request,
        "staff/home.html",
        {
            "date": date,
            "prep": prep,
            "total_kg": total_kg,
            "windows": _service_windows(date),
            "can_scan": user_can(request.user, "checkin.scan"),
            "feedback": StaffFeedback.objects.filter(author=request.user)[:10],
            "kinds": StaffFeedback.Kind.choices,
            "active": "staff",
        },
    )


@perm_required("checkin.scan")
def staff_scan(request):
    date = today()
    meal_slots = list(MealSlot.objects.all())
    chosen = None
    if request.method == "POST":
        raw_meal = request.POST.get("meal") or ""
        chosen = next((slot for slot in meal_slots if slot.name == raw_meal), None)
        if raw_meal and chosen is None:
            messages.error(request, "Unknown meal.")
            return redirect(_next_url(request) or "web:staff-scan")
        token = (request.POST.get("pass_token") or "").strip()[:2048]
        code = (request.POST.get("code") or "").strip()
        try:
            if token:
                student, slot, _checkin = redeem_pass(token, meal=chosen, date=date)
            elif code:
                student, slot, _checkin = manual_checkin(code, meal=chosen, date=date)
            else:
                raise PassError("Scan a pass or type a student code.", "empty")
        except PassError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Checked in {target_code(student)} for {slot.get_name_display()}.",
            )
        return redirect(_next_url(request) or "web:staff-scan")

    slots = []
    for slot in meal_slots:
        slots.append(
            {
                "slot": slot,
                "checkins": CheckIn.objects.filter(date=date, meal=slot).count(),
            }
        )
    recent = [
        {"checkin": checkin, "code": target_code(checkin.student)}
        for checkin in CheckIn.objects.filter(date=date)
        .select_related("meal", "student__profile")
        .order_by("-timestamp")[:10]
    ]
    return render(
        request,
        "staff/scan.html",
        {
            "date": date,
            "slots": slots,
            "meal_slots": meal_slots,
            "open_slot": open_meal(date),
            "recent": recent,
            "today_total": sum(row["checkins"] for row in slots),
            "can_manual": user_can(request.user, "checkin.manual"),
            "active": "scan",
        },
    )


@perm_required("feedback.review")
def supervisor_home(request):
    date = today()
    feedback = (
        StaffFeedback.objects.select_related("author", "author__role_profile")
        .order_by("-created_at")[:50]
    )
    prep = _prep_sheet(date)
    served = {
        row["meal"].pk: CheckIn.objects.filter(date=date, meal=row["meal"]).count()
        for row in prep
    }
    for row in prep:
        row["total_kg"] = sum(float(plan.recommended_kg) for plan in row["dishes"])
        row["served"] = served.get(row["meal"].pk, 0)
    return render(
        request,
        "supervisor/home.html",
        {
            "date": date,
            "feedback": feedback,
            "open_count": StaffFeedback.objects.filter(
                status=StaffFeedback.Status.OPEN
            ).count(),
            "open_by_kind": [
                (
                    label,
                    StaffFeedback.objects.filter(
                        status=StaffFeedback.Status.OPEN, kind=value
                    ).count(),
                )
                for value, label in StaffFeedback.Kind.choices
            ],
            "prep": prep,
            "live": [live_meal_status(date, slot) for slot in MealSlot.objects.all()],
            "statuses": StaffFeedback.Status.choices,
            "active": "supervisor",
        },
    )


@require_POST
@perm_required("feedback.review")
def supervisor_feedback_update(request, pk: int):
    item = get_object_or_404(StaffFeedback, pk=pk)
    new_status = request.POST.get("status", item.status)
    if new_status in StaffFeedback.Status.values:
        item.status = new_status
    note = (request.POST.get("supervisor_note") or "").strip()
    if note:
        item.supervisor_note = note[:2000]
    item.save(update_fields=["status", "supervisor_note", "updated_at"])
    messages.success(request, "Feedback updated.")
    return redirect("web:supervisor-home")


@perm_required("plan.approve")
def manager_dashboard(request):
    date = _date(request)
    context = dashboard_context(date)
    live = [live_meal_status(date, slot) for slot in MealSlot.objects.all()]
    peaks = {slot.name: peak_analysis(date, slot) for slot in MealSlot.objects.all()}
    context.update({"live": live, "peaks": peaks, "active": "dashboard"})
    return render(request, "manager/dashboard.html", context)


@perm_required("plan.view")
def manager_plan(request):
    date = _date(request)
    context = dashboard_context(date)
    context["decisions"] = (
        PlanDecisionLog.objects.filter(plan__date=date)
        .select_related("plan__dish", "plan__meal", "actor")
        .order_by("-at")[:12]
    )
    context["prev_date"] = date - dt.timedelta(days=1)
    context["next_date"] = date + dt.timedelta(days=1)
    context["active"] = "plan"
    return render(request, "manager/plan.html", context)


@perm_required("live.view")
def manager_live(request):
    date = _date(request)
    slot = _meal(request)
    live = [live_meal_status(date, s) for s in MealSlot.objects.all()]
    selected = next((x for x in live if x["meal"].pk == slot.pk), live[0] if live else None)
    peak = peak_analysis(date, slot) if slot else None
    return render(
        request,
        "manager/live.html",
        {
            "date": date,
            "slots": MealSlot.objects.all(),
            "selected": selected,
            "live": live,
            "peak": peak,
            "active": "live",
        },
    )


@perm_required("waste.view")
def manager_waste(request):
    days = int(request.GET.get("days", 30) or 30)
    data = waste_analytics(max(1, min(365, days)))
    return render(request, "manager/waste.html", {"data": data, "active": "waste"})


@perm_required("waste.write")
def manager_waste_entry(request):
    """Record what the kitchen prepared, served and threw away for one dish."""
    date = _date(request, "date")
    initial = {"date": date, "leftover_reusable": False}
    form = WasteEntryForm(initial=initial)
    if request.method == "POST":
        form = WasteEntryForm(request.POST)
        if form.is_valid():
            try:
                log = record_waste(
                    form.cleaned_data["date"],
                    form.cleaned_data["meal"],
                    form.cleaned_data["dish"],
                    form.cleaned_data["prepared_kg"],
                    form.cleaned_data["consumed_kg"],
                    form.cleaned_data["wasted_kg"],
                    leftover_reusable=form.cleaned_data["leftover_reusable"],
                    actor=request.user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Waste recorded for {log.dish.name} at {log.meal.get_name_display()}.",
                )
                return redirect("web:manager-waste-entry")
        date = form.cleaned_data.get("date") or date
    recent = (
        WasteLog.objects.filter(date=date)
        .select_related("meal", "dish")
        .order_by("meal__order", "dish__name")
    )
    return render(
        request,
        "manager/waste_entry.html",
        {"form": form, "date": date, "recent": recent, "active": "waste"},
    )


@perm_required("service.close")
def manager_close_day(request):
    """Review the day and freeze it into meal-service records for training."""
    date = parse_date(request.POST.get("date", "")) if request.method == "POST" else None
    date = date or _date(request, "date")
    if request.method == "POST":
        try:
            summary = close_service_day(
                date,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Closed {summary['date']:%d %b}: {summary['meals']} meals, "
                f"{summary['checkins']} check-ins, {summary['wasted_kg']} kg waste.",
            )
            return redirect(f"{reverse('web:manager-close-day')}?date={date.isoformat()}")
    return render(
        request,
        "manager/close_day.html",
        {
            "date": date,
            "rows": service_day_summary(date),
            "active": "close",
        },
    )


@perm_required("impact.view")
def manager_impact(request):
    days = int(request.GET.get("days", 30) or 30)
    data = impact_summary(max(1, min(365, days)))
    return render(request, "manager/impact.html", {"data": data, "active": "impact"})


@perm_required("menu.manage")
def manager_menu(request):
    days = int(request.GET.get("days", 30) or 30)
    data = menu_insights(max(1, min(365, days)))
    return render(request, "manager/menu.html", {"data": data, "active": "menu"})


@perm_required("menu.manage")
def manager_menu_day(request):
    """Read-only list of every meal published for one day."""
    date = _date(request)
    menus = (
        DailyMenu.objects.filter(date=date)
        .select_related("meal")
        .prefetch_related("items__dish")
        .order_by("meal__order", "meal__start_time")
    )
    rows = []
    for menu in menus:
        rows.append(
            {
                "menu": menu,
                "items": list(menu.items.select_related("dish")),
                "feedback": feedback_counts(date, menu.meal),
                "suggestions": list(
                    MealFeedback.objects.filter(date=date, meal=menu.meal)
                    .exclude(suggestion="")
                    .select_related("student__profile")
                    .order_by("-updated_at")[:20]
                ),
            }
        )
    return render(
        request,
        "manager/menu_day.html",
        {"date": date, "rows": rows, "active": "menu-day"},
    )


@perm_required("menu.manage")
def manager_data(request):
    """Add or edit the reference data a real mess needs: meal slots and dishes."""
    editing_slot = None
    slot_pk = request.POST.get("slot_id") or request.GET.get("slot")
    if slot_pk:
        editing_slot = get_object_or_404(MealSlot, pk=slot_pk)
    slot_form = MealSlotForm(instance=editing_slot)
    dish_form = DishForm()
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "slot-save":
            slot_form = MealSlotForm(request.POST, instance=editing_slot)
            if slot_form.is_valid():
                slot = slot_form.save()
                AccountAudit.objects.create(
                    actor=request.user,
                    action=AccountAudit.Action.SLOT_SAVE,
                    detail={
                        "meal_slot_id": slot.pk,
                        "name": slot.name,
                        "start": slot.start_time.strftime("%H:%M"),
                        "end": slot.end_time.strftime("%H:%M"),
                    },
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, f"Meal slot “{slot.get_name_display()}” saved.")
                return redirect("web:manager-data")
            error = "Check the meal-slot fields."
        elif action == "dish-save":
            dish_form = DishForm(request.POST)
            if dish_form.is_valid():
                try:
                    dish = save_dish(
                        dish_form.cleaned_data,
                        actor=request.user,
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                except MenuError as exc:
                    error = str(exc)
                else:
                    messages.success(request, f"Dish “{dish.name}” saved.")
                    return redirect("web:manager-data")
            else:
                error = "Check the dish fields."
    return render(
        request,
        "manager/data.html",
        {
            "active": "data",
            "slot_form": slot_form,
            "dish_form": dish_form,
            "editing_slot": editing_slot,
            "slots": MealSlot.objects.order_by("order", "start_time"),
            "dishes": Dish.objects.order_by("category", "name"),
            "error": error,
        },
    )


@perm_required("menu.manage")
def manager_menu_manage(request):
    date = _date(request)
    meal = _meal(request)
    editing_dish = None
    dish_pk = request.POST.get("dish_id") or request.GET.get("dish")
    if dish_pk:
        editing_dish = get_object_or_404(Dish, pk=dish_pk)

    dish_form = DishForm(instance=editing_dish)
    meta_form = MenuMetaForm(initial={"date": date, "meal": meal})
    error = ""

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "dish-save":
            dish_form = DishForm(request.POST, instance=editing_dish)
            if dish_form.is_valid():
                try:
                    dish = save_dish(
                        dish_form.cleaned_data,
                        instance=editing_dish,
                        actor=request.user,
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                except MenuError as exc:
                    error = str(exc)
                else:
                    messages.success(request, f"Dish “{dish.name}” saved.")
                    return redirect(
                        f"{reverse('web:manager-menu-manage')}?dish={dish.pk}"
                    )
            else:
                error = "Check the dish fields."
        elif action == "dish-toggle":
            dish = get_object_or_404(Dish, pk=request.POST.get("dish_id"))
            toggle_dish(
                dish,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Dish “{dish.name}” {'activated' if dish.is_active else 'deactivated'}.",
            )
            return redirect("web:manager-menu-manage")
        elif action == "menu-save":
            raw_date = parse_date(request.POST.get("date", ""))
            if raw_date:
                date = raw_date
            posted_meal = MealSlot.objects.filter(pk=request.POST.get("meal")).first()
            if posted_meal:
                meal = posted_meal
            meta_form = MenuMetaForm(request.POST)
            if meta_form.is_valid():
                date = meta_form.cleaned_data["date"]
                meal = meta_form.cleaned_data["meal"]
                items = []
                details = {}
                for dish in Dish.objects.all():
                    raw = (request.POST.get(f"qty_{dish.pk}") or "").strip()
                    if not raw:
                        continue
                    special = request.POST.get(f"special_{dish.pk}") == "on"
                    items.append((dish, raw, special))
                    details[dish.pk] = {
                        "serving_category": (
                            request.POST.get(f"category_{dish.pk}") or "unlimited"
                        ),
                        "portion_grams": (
                            request.POST.get(f"portion_{dish.pk}") or ""
                        ).strip()
                        or None,
                        "frequency_per_month": (
                            request.POST.get(f"frequency_{dish.pk}") or ""
                        ).strip()
                        or None,
                    }
                try:
                    save_daily_menu(
                        date=date,
                        meal=meal,
                        items=items,
                        notes=meta_form.cleaned_data["notes"],
                        is_special=meta_form.cleaned_data["is_special"],
                        details=details,
                        actor=request.user,
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                except MenuError as exc:
                    error = str(exc)
                else:
                    messages.success(
                        request,
                        f"Menu saved for {date:%d %b} · {meal.get_name_display()}.",
                    )
                    return redirect(
                        f"{reverse('web:manager-menu-manage')}"
                        f"?date={date.isoformat()}&meal={meal.name}"
                    )
            else:
                error = "Check the date and meal."

    menu = (
        DailyMenu.objects.filter(date=date, meal=meal)
        .prefetch_related("items__dish")
        .first()
    )
    existing = {item.dish_id: item for item in menu.items.all()} if menu else {}
    dishes = (
        Dish.objects.filter(Q(is_active=True) | Q(pk__in=existing.keys()))
        .order_by("category", "name")
    )
    rows = [
        {
            "dish": dish,
            "quantity": existing[dish.pk].planned_quantity_kg
            if dish.pk in existing
            else "",
            "special": existing[dish.pk].is_special if dish.pk in existing else False,
            "category": (
                existing[dish.pk].serving_category
                if dish.pk in existing
                else "unlimited"
            ),
            "portion": existing[dish.pk].portion_grams
            if dish.pk in existing
            else "",
            "frequency": existing[dish.pk].frequency_per_month
            if dish.pk in existing
            else "",
        }
        for dish in dishes
    ]
    return render(
        request,
        "manager/menu_manage.html",
        {
            "active": "menu-manage",
            "date": date,
            "meal": meal,
            "meal_slots": MealSlot.objects.order_by("order"),
            "menu": menu,
            "rows": rows,
            "dish_form": dish_form,
            "meta_form": meta_form,
            "editing_dish": editing_dish,
            "error": error,
        },
    )


@perm_required("model.manage")
def manager_model(request):
    health = model_health()
    return render(request, "manager/model.html", {"health": health, "active": "model"})


@require_POST
@perm_required("plan.approve")
def manager_approve(request, pk: int):
    plan = get_object_or_404(PreparationPlan, pk=pk)
    plan.status = PreparationPlan.Status.APPROVED
    plan.approved_by = request.user
    plan.approved_at = timezone.now()
    plan.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    PlanDecisionLog.objects.create(
        plan=plan,
        action=PlanDecisionLog.Action.APPROVE,
        reason=request.POST.get("reason", "")[:1000],
        actor=request.user,
    )
    messages.success(request, f"Approved {plan.dish.name} for {plan.meal.get_name_display()}.")
    return redirect(request.POST.get("next") or reverse("web:manager-dashboard"))


@require_POST
@perm_required("plan.override")
def manager_override(request, pk: int):
    plan = get_object_or_404(PreparationPlan, pk=pk)
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "A reason is required for an override.")
        return redirect(request.POST.get("next") or reverse("web:manager-dashboard"))
    raw = request.POST.get("recommended_kg")
    if raw:
        try:
            value = Decimal(raw)
            if value < 0:
                raise InvalidOperation
            plan.recommended_kg = value
        except (InvalidOperation, ValueError):
            messages.error(request, "Recommended quantity must be a positive number.")
            return redirect(request.POST.get("next") or reverse("web:manager-dashboard"))
    plan.status = PreparationPlan.Status.OVERRIDDEN
    plan.override_reason = reason
    plan.approved_by = request.user
    plan.approved_at = timezone.now()
    plan.save(
        update_fields=[
            "recommended_kg",
            "status",
            "override_reason",
            "approved_by",
            "approved_at",
            "updated_at",
        ]
    )
    PlanDecisionLog.objects.create(
        plan=plan,
        action=PlanDecisionLog.Action.OVERRIDE,
        reason=reason,
        actor=request.user,
    )
    messages.success(request, f"Override saved for {plan.dish.name} and logged.")
    return redirect(request.POST.get("next") or reverse("web:manager-dashboard"))


@require_POST
@perm_required("plan.approve")
def manager_approve_all(request):
    """Approve every draft dish for one meal in a single tap."""
    date = parse_date(request.POST.get("date", "")) or today()
    slot = get_object_or_404(MealSlot, name=request.POST.get("meal", ""))
    plans = list(
        PreparationPlan.objects.filter(
            date=date, meal=slot, status=PreparationPlan.Status.DRAFT
        ).select_related("dish")
    )
    if not plans:
        messages.info(request, f"No draft dishes left for {slot.get_name_display()}.")
        return redirect(request.POST.get("next") or reverse("web:manager-plan"))
    now = timezone.now()
    with transaction.atomic():
        for plan in plans:
            plan.status = PreparationPlan.Status.APPROVED
            plan.approved_by = request.user
            plan.approved_at = now
            plan.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
            PlanDecisionLog.objects.create(
                plan=plan,
                action=PlanDecisionLog.Action.APPROVE,
                reason="Bulk approved from the preparation plan.",
                actor=request.user,
            )
    messages.success(
        request, f"Approved {len(plans)} dish(es) for {slot.get_name_display()} on {date:%d %b}."
    )
    return redirect(request.POST.get("next") or reverse("web:manager-plan"))


@require_POST
@perm_required("model.manage")
def manager_retrain(request):
    """Retrain from the latest closed days, then regenerate draft plans."""
    from forecasting.model import InsufficientData
    from forecasting.pipeline import train_and_store

    try:
        run = train_and_store(validation_days=21, generate=True, days=8)
    except InsufficientData as exc:
        messages.error(request, f"Not enough history to retrain: {exc}")
    except Exception as exc:  # keep the demo usable if sklearn rejects a tiny sample
        messages.error(request, f"Retraining failed: {exc}")
    else:
        messages.success(
            request,
            f"Trained {run.version}: participation MAE {run.participation_mae:.1f} "
            f"({run.participation_improvement_pct:.1f}% better than naive), "
            f"dish MAE {run.dish_mae_kg:.2f} kg. Draft plans regenerated.",
        )
    return redirect("web:manager-model")


@perm_required("plan.view")
def manager_export(request):
    """Download the numbers behind a manager page as a spreadsheet-friendly CSV."""
    kind = request.GET.get("kind", "plan")
    date = _date(request)
    days = max(1, min(365, int(request.GET.get("days", 30) or 30)))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="messlab-{kind}-{date:%Y%m%d}.csv"'
    )
    response.write("\ufeff")  # Excel-friendly UTF-8 BOM
    writer = csv.writer(response)

    if kind == "plan":
        writer.writerow(
            [
                "date",
                "meal",
                "dish",
                "predicted_demand_kg",
                "baseline_kg",
                "recommended_kg",
                "planned_saving_kg",
                "status",
                "approved_by",
                "override_reason",
            ]
        )
        forecasts = {
            (f.meal_id, f.dish_id): f
            for f in Forecast.objects.filter(date=date, dish__isnull=False)
        }
        plans = (
            PreparationPlan.objects.filter(date=date)
            .select_related("meal", "dish", "approved_by")
            .order_by("meal__order", "dish__name")
        )
        for plan in plans:
            forecast = forecasts.get((plan.meal_id, plan.dish_id))
            writer.writerow(
                [
                    plan.date.isoformat(),
                    plan.meal.name,
                    plan.dish.name,
                    f"{forecast.predicted_demand_kg:.2f}" if forecast else "",
                    f"{plan.baseline_kg:.2f}",
                    f"{plan.recommended_kg:.2f}",
                    f"{plan.planned_waste_avoided_kg:.2f}",
                    plan.status,
                    plan.approved_by.username if plan.approved_by else "",
                    plan.override_reason,
                ]
            )
    elif kind == "waste":
        start = date - dt.timedelta(days=days - 1)
        writer.writerow(
            [
                "date",
                "meal",
                "dish",
                "prepared_kg",
                "consumed_kg",
                "wasted_kg",
                "waste_pct",
                "cost_inr",
                "reusable_leftover",
            ]
        )
        logs = (
            WasteLog.objects.filter(date__gte=start, date__lte=date)
            .select_related("meal", "dish")
            .order_by("date", "meal__order", "dish__name")
        )
        cost = settings.MESSLAB["COST_PER_KG_INR"]
        for log in logs:
            writer.writerow(
                [
                    log.date.isoformat(),
                    log.meal.name,
                    log.dish.name,
                    f"{log.prepared_kg:.2f}",
                    f"{log.consumed_kg:.2f}",
                    f"{log.wasted_kg:.2f}",
                    f"{log.waste_pct:.1f}",
                    f"{float(log.wasted_kg) * cost:.0f}",
                    "yes" if log.leftover_reusable else "no",
                ]
            )
    elif kind == "impact":
        start = date - dt.timedelta(days=days - 1)
        writer.writerow(
            [
                "date",
                "meal",
                "dish",
                "baseline_kg",
                "recommended_kg",
                "consumed_kg",
                "waste_without_kg",
                "waste_with_kg",
                "avoided_kg",
            ]
        )
        logs = {
            (l.date, l.meal_id, l.dish_id): l
            for l in WasteLog.objects.filter(date__gte=start, date__lte=date)
        }
        plans = (
            PreparationPlan.objects.filter(date__gte=start, date__lte=date)
            .select_related("meal", "dish")
            .order_by("date", "meal__order", "dish__name")
        )
        for plan in plans:
            log = logs.get((plan.date, plan.meal_id, plan.dish_id))
            if not log:
                continue
            baseline = float(plan.baseline_kg) or float(log.prepared_kg)
            recommended = float(plan.recommended_kg)
            consumed = float(log.consumed_kg)
            writer.writerow(
                [
                    plan.date.isoformat(),
                    plan.meal.name,
                    plan.dish.name,
                    f"{baseline:.2f}",
                    f"{recommended:.2f}",
                    f"{consumed:.2f}",
                    f"{max(0.0, baseline - consumed):.2f}",
                    f"{max(0.0, recommended - consumed):.2f}",
                    f"{max(0.0, baseline - recommended):.2f}",
                ]
            )
    elif kind == "menu":
        data = menu_insights(days)
        writer.writerow(
            [
                "dish",
                "category",
                "diet",
                "times_served",
                "consumed_kg",
                "wasted_kg",
                "waste_pct",
                "uptake_pct",
                "shortages",
                "portion_grams",
            ]
        )
        for row in data["rows"]:
            writer.writerow(
                [
                    row["dish"],
                    row["category"],
                    row["diet"],
                    row["served"],
                    f"{row['consumed_kg']:.2f}",
                    f"{row['wasted_kg']:.2f}",
                    f"{row['waste_pct']:.1f}",
                    f"{row['uptake_pct']:.1f}",
                    row["shortages"],
                    row["portion_grams"],
                ]
            )
    else:
        return HttpResponse("Unknown export type.", status=400, content_type="text/plain")
    return response


@perm_required("import.csv")
def manager_import(request):
    if request.method == "POST":
        kind = request.POST.get("kind", "")
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Attach a CSV file.")
        elif upload.size > MAX_CSV_BYTES:
            messages.error(
                request,
                f"That file is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB.",
            )
        else:
            try:
                raw = upload.read()
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    content = raw.decode("latin-1")
                result = import_csv_file(
                    kind=kind,
                    filename=upload.name,
                    content=content,
                    user=request.user,
                    dry_run=request.POST.get("dry_run") == "on",
                    allow_partial=request.POST.get("allow_partial") == "on",
                )
                messages.success(
                    request,
                    f"{'Validated' if result.dry_run else 'Imported'} {result.kind}: "
                    f"{result.created} created, {result.updated} updated.",
                )
                if result.errors:
                    messages.warning(request, f"{len(result.errors)} row warnings were recorded.")
            except CsvImportError as exc:
                messages.error(request, "Import failed. " + " ".join(exc.errors[:5]))
    return render(
        request,
        "manager/import.html",
        {
            "active": "import",
            "kinds": [
                ("participation", "Participation / intents"),
                ("menus", "Menus"),
                ("prepared", "Quantities prepared"),
                ("wastage", "Wastage"),
            ],
            "recent": DataImport.objects.select_related("imported_by")[:12],
        },
    )


@perm_required("users.view")
def manager_users(request):
    qs = User.objects.select_related("role_profile", "profile").order_by("username")
    viewer_is_admin = user_can(request.user, "users.role")
    if role_of(request.user) == User.Role.SUPERVISOR:
        qs = qs.filter(
            role_profile__role__in=[User.Role.WORKER, User.Role.CANTEEN_STAFF]
        )

    role_filter = request.GET.get("role", "")
    if role_filter in User.Role.values:
        qs = qs.filter(role_profile__role=role_filter)
    status_filter = request.GET.get("status", "")
    if status_filter == "active":
        qs = qs.filter(is_active=True)
    elif status_filter == "inactive":
        qs = qs.filter(is_active=False)

    query = (request.GET.get("q") or "").strip()
    if query:
        qs = qs.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
            | Q(profile__student_code__icontains=query)
        )

    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = []
    for member in page.object_list:
        can_manage = can_manage_target(request.user, member)
        rows.append(
            {
                "user": member,
                "code": target_code(member),
                "role": role_of(member),
                "role_label": User.Role(role_of(member)).label
                if role_of(member) in User.Role.values
                else role_of(member),
                "email": member.email if viewer_is_admin else mask_email(member.email),
                "name": (
                    f"{member.first_name} {member.last_name}".strip()
                    if viewer_is_admin
                    else ""
                ),
                "active": member.is_active,
                "pending": getattr(member.role_profile, "is_pending", False),
                "can_manage": can_manage,
            }
        )

    return render(
        request,
        "manager/users.html",
        {
            "active": "users",
            "rows": rows,
            "page": page,
            "total": page.paginator.count,
            "role_filter": role_filter,
            "status_filter": status_filter,
            "query": query,
            "role_choices": User.Role.choices,
            "viewer_is_admin": viewer_is_admin,
            "can_change_roles": user_can(request.user, "users.role"),
            "can_manage": user_can(request.user, "users.manage"),
        },
    )


@require_POST
@perm_required("users.role")
def manager_user_role(request, pk: int):
    target = get_object_or_404(User, pk=pk)
    new_role = request.POST.get("role", "")
    try:
        changed = change_role(request.user, target, new_role, request)
    except AccountAdminError as exc:
        messages.error(request, str(exc))
        return redirect("web:manager-users")
    if changed:
        messages.success(
            request, f"{target_code(target)} is now {User.Role(new_role).label}."
        )
    else:
        messages.info(request, "That role is already set.")
    return redirect("web:manager-users")


@require_POST
@perm_required("users.manage")
def manager_user_active(request, pk: int):
    target = get_object_or_404(User, pk=pk)
    make_active = request.POST.get("active") == "1"
    try:
        changed = set_active(request.user, target, make_active, request)
    except AccountAdminError as exc:
        messages.error(request, str(exc))
        return redirect("web:manager-users")
    if changed:
        messages.success(
            request,
            f"{target_code(target)} {'activated' if make_active else 'deactivated'}.",
        )
    else:
        messages.info(request, "That account is already in this state.")
    return redirect("web:manager-users")


@perm_required("audit.view")
def manager_audit(request):
    qs = AccountAudit.objects.select_related(
        "actor__profile", "target__profile"
    ).order_by("-at")
    action_filter = request.GET.get("action", "")
    if action_filter in AccountAudit.Action.values:
        qs = qs.filter(action=action_filter)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    rows = [
        {
            "entry": entry,
            "actor": target_code(entry.actor),
            "target": target_code(entry.target),
            "detail": ", ".join(f"{k}={v}" for k, v in (entry.detail or {}).items()),
        }
        for entry in page.object_list
    ]
    return render(
        request,
        "manager/audit.html",
        {
            "active": "audit",
            "rows": rows,
            "page": page,
            "total": page.paginator.count,
            "action_filter": action_filter,
            "action_choices": AccountAudit.Action.choices,
        },
    )


def manifest(request):
    path = Path(settings.BASE_DIR) / "static" / "manifest.webmanifest"
    return HttpResponse(path.read_text(encoding="utf-8"), content_type="application/manifest+json")


def service_worker(request):
    path = Path(settings.BASE_DIR) / "static" / "sw.js"
    response = HttpResponse(path.read_text(encoding="utf-8"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response


def offline(request):
    return render(request, "offline.html")
