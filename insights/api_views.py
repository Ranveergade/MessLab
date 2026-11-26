"""Versioned DRF API. Students touch only their own data; managers see aggregates."""
from __future__ import annotations

import csv
import datetime as dt
import io

from django.conf import settings
from django.contrib.auth import login as session_login, logout as session_logout
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.account_admin import AccountAdminError, change_role, set_active, target_code
from accounts.authentication import issue_tokens
from accounts.models import AccountAudit, LoginEvent, User
from accounts.otp import request_login_otp, verify_otp
from accounts.permissions import HasPerm
from accounts.rbac import landing_for, role_of, user_can
from accounts.registration import complete_registration, start_registration
from dining.importers import MAX_CSV_BYTES, CsvImportError, import_csv_file
from dining.passes import (
    PassError,
    issue_pass,
    manual_checkin,
    pass_meal,
    pass_svg,
    redeem_pass,
)
from dining.models import (
    CheckIn,
    DayContext,
    Forecast,
    MealIntent,
    MealService,
    PlanDecisionLog,
    PreparationPlan,
    WasteLog,
)
from dining.services import (
    check_in_student,
    close_service_day,
    declared_positive_intents,
    get_intent_view,
    intent_counts,
    meal_window,
    record_waste,
    save_meal_feedback,
    set_intent,
    today,
)
from forecasting.model import InsufficientData
from forecasting.pipeline import train_and_store
from menu.models import DailyMenu, Dish, MealSlot, MenuItem
from menu.services import (
    MenuError,
    delete_dish,
    delete_menu,
    menu_queryset,
    save_daily_menu,
    save_dish,
)
from rewards.models import RewardEvent
from rewards.services import hostel_leaderboard, student_summary

from .privacy import delete_person, export_person
from .serializers import (
    AccountAuditSerializer,
    ActiveUpdateSerializer,
    AdminUserSerializer,
    CheckInCreateSerializer,
    CheckInSerializer,
    CloseDaySerializer,
    DailyMenuSerializer,
    DailyMenuWriteSerializer,
    DefaultPlanSerializer,
    DishWriteSerializer,
    ImportSerializer,
    IntentUpsertSerializer,
    LogoutSerializer,
    ManualCheckInSerializer,
    MealFeedbackSerializer,
    MealFeedbackWriteSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
    OverrideSerializer,
    PassResponseSerializer,
    RegistrationSerializer,
    RoleUpdateSerializer,
    ScanSerializer,
    UserSerializer,
    WasteLogSerializer,
    WasteLogWriteSerializer,
)
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


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200


def _date_param(request, key: str = "date", default=None) -> dt.date:
    raw = request.query_params.get(key)
    if not raw:
        return default or today()
    parsed = parse_date(raw)
    if not parsed:
        raise ValidationError({key: "Use an ISO date (YYYY-MM-DD)."})
    return parsed


def _optional_date_param(request, key: str):
    raw = request.query_params.get(key)
    if not raw:
        return None
    parsed = parse_date(raw)
    if not parsed:
        raise ValidationError({key: "Use an ISO date (YYYY-MM-DD)."})
    return parsed


def _int_param(request, key: str, default: int, minimum: int = 1, maximum: int = 365) -> int:
    try:
        value = int(request.query_params.get(key, default))
    except (TypeError, ValueError):
        raise ValidationError({key: "Must be an integer."})
    return max(minimum, min(maximum, value))


def _meal_param(request, key: str = "meal") -> MealSlot | None:
    name = request.query_params.get(key)
    if not name:
        return None
    slot = MealSlot.objects.filter(name=name.lower()).first()
    if not slot:
        raise ValidationError({key: f"Unknown meal {name!r}."})
    return slot


def _slot_dict(slot: MealSlot) -> dict:
    return {
        "id": slot.pk,
        "name": slot.name,
        "label": slot.get_name_display(),
        "start": slot.start_time.strftime("%H:%M"),
        "end": slot.end_time.strftime("%H:%M"),
        "peak": slot.peak_label,
        "counters": slot.counters,
        "hourly_capacity": slot.hourly_capacity,
    }


def _meal_payload(data: dict) -> dict:
    """Replace model instances with API-friendly dicts for JSON rendering."""
    payload = dict(data)
    if "meal" in payload and isinstance(payload["meal"], MealSlot):
        payload["meal"] = _slot_dict(payload["meal"])
    return payload


def _intent_dict(view: dict) -> dict:
    return {
        "status": view["status"],
        "source": view["source"],
        "expected_arrival_window": view["expected_arrival_window"],
        "is_explicit": view["is_explicit"],
        "plan_value": view.get("plan_value", "auto"),
        "updated_at": view.get("updated_at"),
    }


def _badge_dict(badge) -> dict | None:
    if not badge:
        return None
    return {
        "code": badge.code,
        "name": badge.name,
        "icon": badge.icon,
        "description": badge.description,
        "points": badge.points,
    }


def _rewards_payload(summary: dict) -> dict:
    data = dict(summary)
    data["badges"] = [_badge_dict(badge) for badge in summary["badges"]]
    data["next_badge"] = _badge_dict(summary["next_badge"])
    return data


def _checkin_dict(checkin: CheckIn | None) -> dict | None:
    if not checkin:
        return None
    return {
        "id": checkin.pk,
        "timestamp": checkin.timestamp,
        "clean_plate": checkin.clean_plate,
        "dishes": [d.name for d in checkin.dishes_taken.all()],
        "source": checkin.source,
    }


def _meal_card(card: dict) -> dict:
    slot = card["slot"]
    menu = card["menu"]
    if not menu:
        return {
            "meal": _slot_dict(slot),
            "menu": None,
            "crowd": None,
            "intent": None,
            "checkin": None,
            "nudge": card["nudge"],
            "allergy_note": card["allergy_note"],
        }
    return {
        "meal": _slot_dict(slot),
        "menu": None
        if not menu
        else {
            "id": menu.pk,
            "notes": menu.notes,
            "is_special": menu.is_special,
            "items": [
                {
                    "id": item.dish.pk,
                    "name": item.dish.name,
                    "category": item.dish.category,
                    "category_label": item.dish.get_category_display(),
                    "diet_type": item.dish.diet_type,
                    "diet_label": item.dish.get_diet_type_display(),
                    "avg_portion_grams": item.dish.avg_portion_grams,
                    "popularity": item.dish.popularity_baseline,
                    "is_special": item.is_special,
                    "planned_kg": float(item.planned_quantity_kg),
                    "allowed_for_me": card["diet_allows"].get(item.dish_id, True),
                }
                for item in card["items"]
            ],
        },
        "crowd": _meal_payload(card["crowd"]),
        "intent": _intent_dict(card["intent"]),
        "checkin": _checkin_dict(card["checkin"]),
        "nudge": card["nudge"],
        "allergy_note": card["allergy_note"],
    }


def _plan_rows(date: dt.date) -> tuple[list[dict], dict]:
    rows = []
    totals = {"recommended_kg": 0.0, "baseline_kg": 0.0, "saved_kg": 0.0, "dishes": 0}
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
        counts = intent_counts(date, slot)
        for plan in plans:
            fc = forecasts.get(plan.dish_id)
            recommended = float(plan.recommended_kg)
            baseline = float(plan.baseline_kg)
            totals["recommended_kg"] += recommended
            totals["baseline_kg"] += baseline
            totals["saved_kg"] += max(0.0, baseline - recommended)
            totals["dishes"] += 1
            rows.append(
                {
                    "id": plan.pk,
                    "date": date,
                    "meal": slot.name,
                    "meal_label": slot.get_name_display(),
                    "dish": plan.dish.name,
                    "dish_id": plan.dish_id,
                    "recommended_kg": recommended,
                    "baseline_kg": baseline,
                    "actual_prepared_kg": float(plan.actual_prepared_kg)
                    if plan.actual_prepared_kg is not None
                    else None,
                    "projected_waste_avoided_kg": round(max(0.0, baseline - recommended), 2),
                    "status": plan.status,
                    "override_reason": plan.override_reason,
                    "approved_at": plan.approved_at,
                    "forecast": {
                        "predicted_participation": fc.predicted_participation if fc else None,
                        "predicted_demand_kg": float(fc.predicted_demand_kg) if fc else None,
                        "lower_bound_kg": float(fc.lower_bound_kg) if fc else None,
                        "upper_bound_kg": float(fc.upper_bound_kg) if fc else None,
                        "lower_participation": fc.lower_participation if fc else None,
                        "upper_participation": fc.upper_participation if fc else None,
                        "model_version": fc.model_version if fc else None,
                    },
                    "meal_forecast": {
                        "predicted_participation": meal_forecast.predicted_participation
                        if meal_forecast
                        else None,
                        "lower_participation": meal_forecast.lower_participation
                        if meal_forecast
                        else None,
                        "upper_participation": meal_forecast.upper_participation
                        if meal_forecast
                        else None,
                    },
                    "intents": counts,
                }
            )
    totals["recommended_kg"] = round(totals["recommended_kg"], 1)
    totals["baseline_kg"] = round(totals["baseline_kg"], 1)
    totals["saved_kg"] = round(totals["saved_kg"], 1)
    return rows, totals


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@extend_schema(request=OTPRequestSerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_otp_request(request):
    serializer = OTPRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    request_login_otp(serializer.validated_data["email"], request=request)
    return Response(
        {
            "detail": "If this email is registered, a code has been sent.",
            "status": "sent",
        }
    )


@extend_schema(request=OTPVerifySerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_otp_verify(request):
    serializer = OTPVerifySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]
    code = serializer.validated_data["code"]
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    result = verify_otp(user, code, request=request)
    if result.status != "ok" or not result.user:
        return Response(
            {"detail": "Invalid or expired code.", "status": result.status},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_login(request, result.user)
    tokens = issue_tokens(result.user)
    return Response(
        {
            **tokens,
            "user": UserSerializer(result.user).data,
            "redirect": landing_for(result.user),
        }
    )


@extend_schema(request=LogoutSerializer, responses={204: None})
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_logout(request):
    serializer = LogoutSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    refresh = serializer.validated_data.get("refresh")
    if refresh:
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            pass
    if request.user.is_authenticated:
        LoginEvent.objects.create(
            user=request.user,
            event=LoginEvent.Event.LOGOUT,
            role=request.user.effective_role,
            success=True,
        )
        session_logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses={200: UserSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def auth_me(request):
    return Response(UserSerializer(request.user).data)


@extend_schema(request=RegistrationSerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_register(request):
    if not settings.MESSLAB.get("ALLOW_SELF_REGISTRATION", True):
        return Response(
            {"detail": "Self-registration is closed."},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = RegistrationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    start_registration(**serializer.validated_data, request=request)
    return Response(
        {
            "detail": "If this email can be registered, a code has been sent.",
            "status": "sent",
        }
    )


@extend_schema(request=OTPVerifySerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_register_verify(request):
    if not settings.MESSLAB.get("ALLOW_SELF_REGISTRATION", True):
        return Response(
            {"detail": "Self-registration is closed."},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = OTPVerifySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    result = complete_registration(
        serializer.validated_data["email"],
        serializer.validated_data["code"],
        request=request,
    )
    if result.status != "ok" or not result.user:
        return Response(
            {"detail": "Invalid or expired code.", "status": result.status},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_login(request, result.user)
    tokens = issue_tokens(result.user)
    return Response(
        {
            **tokens,
            "user": UserSerializer(result.user).data,
            "redirect": landing_for(result.user),
        },
        status=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# Student endpoints
# ---------------------------------------------------------------------------
@extend_schema(methods=["GET"], responses={200: OpenApiTypes.OBJECT})
@extend_schema(methods=["PUT"], request=DefaultPlanSerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["GET", "PUT"])
@permission_classes([HasPerm("plan.weekly")])
def my_plan(request):
    profile = getattr(request.user, "profile", None)
    if not profile:
        return Response({"detail": "Student profile missing."}, status=404)
    if request.method == "GET":
        return Response(
            {
                "default_plan": profile.normalized_plan(),
                "hostel": profile.hostel,
                "diet": profile.diet,
                "allergies": profile.allergies,
                "student_code": profile.student_code,
            }
        )
    serializer = DefaultPlanSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    profile.default_plan = serializer.validated_data["default_plan"]
    profile.save(update_fields=["default_plan", "updated_at"])
    return Response({"default_plan": profile.normalized_plan()})


@extend_schema(methods=["GET"], responses={200: OpenApiTypes.OBJECT})
@extend_schema(methods=["POST"], request=IntentUpsertSerializer, responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT})
@api_view(["GET", "POST"])
@permission_classes([HasPerm("intent.write")])
def intents(request):
    if request.method == "GET":
        date = _date_param(request)
        days = _int_param(request, "days", 7, 1, 21)
        explicit = {
            (i.meal_id): i
            for i in MealIntent.objects.filter(
                student=request.user,
                date__gte=date,
                date__lt=date + dt.timedelta(days=days),
            ).select_related("meal")
        }
        rows = []
        for offset in range(days):
            day = date + dt.timedelta(days=offset)
            for slot in MealSlot.objects.all():
                intent = explicit.get(slot.pk) if offset == 0 else None
                view = (
                    {
                        "status": intent.status,
                        "source": intent.source,
                        "expected_arrival_window": intent.expected_arrival_window,
                        "is_explicit": True,
                    }
                    if intent
                    else get_intent_view(request.user, day, slot)
                )
                rows.append(
                    {
                        "date": day,
                        "meal": slot.name,
                        "status": view["status"],
                        "source": view["source"],
                        "expected_arrival_window": view["expected_arrival_window"],
                        "is_explicit": view["is_explicit"],
                    }
                )
        return Response({"date": date, "days": days, "intents": rows})

    serializer = IntentUpsertSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    date = data["date"]
    lower, upper = today() - dt.timedelta(days=1), today() + dt.timedelta(days=14)
    if not lower <= date <= upper:
        raise ValidationError({"date": f"Intents are accepted between {lower} and {upper}."})
    try:
        intent, created = set_intent(
            request.user,
            date,
            data["meal"],
            data["status"],
            source=data.get("source") or MealIntent.Source.ONE_TAP,
            arrival=data.get("expected_arrival_window") or None,
        )
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    return Response(
        {
            "id": intent.pk,
            "date": intent.date,
            "meal": intent.meal.name,
            "status": intent.status,
            "source": intent.source,
            "expected_arrival_window": intent.expected_arrival_window,
            "created": created,
        },
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("intent.write")])
def meals_today(request):
    date = _date_param(request)
    cards = today_meals(request.user, date)
    return Response(
        {
            "date": date,
            "meals": [_meal_card(card) for card in cards],
            "rewards": _rewards_payload(student_summary(request.user, date)),
        }
    )


@extend_schema(request=CheckInCreateSerializer, responses={201: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("qr.show")])
def check_in(request):
    serializer = CheckInCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    date = data["date"]
    if date < today() - dt.timedelta(days=7) or date > today() + dt.timedelta(days=1):
        raise ValidationError({"date": "Check-ins are open for the current service day."})
    try:
        obj = check_in_student(
            request.user,
            date,
            data["meal"],
            dish_ids=data.get("dish_ids") or [],
            clean_plate=data["clean_plate"],
            source=data.get("source") or CheckIn.Source.QR,
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {
            "checkin": CheckInSerializer(obj).data,
            "rewards": _rewards_payload(student_summary(request.user, date)),
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=MealFeedbackWriteSerializer, responses={200: MealFeedbackSerializer}
)
@api_view(["POST"])
@permission_classes([HasPerm("intent.write")])
def meal_feedback(request):
    serializer = MealFeedbackWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    if abs((data["date"] - today()).days) > 1:
        return Response(
            {"detail": "Feedback is only open for today's menu."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        feedback = save_meal_feedback(
            request.user,
            data["date"],
            data["meal"],
            data["response"],
            data.get("suggestion", ""),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(MealFeedbackSerializer(feedback).data)


def _scan_payload(student, slot: MealSlot, checkin: CheckIn) -> dict:
    """Pseudonymous confirmation shared by the QR and manual scan endpoints."""
    return {
        "student_code": target_code(student),
        "meal": slot.name,
        "date": checkin.date,
        "checkin": CheckInSerializer(checkin).data,
    }


@extend_schema(responses={200: PassResponseSerializer})
@api_view(["GET"])
@permission_classes([HasPerm("qr.show")])
def checkin_pass(request):
    """Issue today's signed pass and its SVG rendering for the signed-in member."""
    date = today()
    slot = pass_meal(date)
    if slot is None:
        return Response(
            {"detail": "Add a meal slot before issuing passes."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    token = issue_pass(request.user, date, slot)
    return Response(
        {"date": date, "meal": slot.name, "token": token, "qr_svg": pass_svg(token)}
    )


@extend_schema(request=ScanSerializer, responses={201: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("checkin.scan")])
def staff_scan(request):
    """Redeem a scanned pass. The signature, day and token version are all checked."""
    serializer = ScanSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        student, slot, checkin = redeem_pass(data["token"], meal=data.get("meal"))
    except PassError as exc:
        return Response(
            {"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
        )
    return Response(_scan_payload(student, slot, checkin), status=status.HTTP_201_CREATED)


@extend_schema(request=ManualCheckInSerializer, responses={201: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("checkin.manual")])
def staff_manual_checkin(request):
    """Check in by typed student code when the camera cannot be used."""
    serializer = ManualCheckInSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        student, slot, checkin = manual_checkin(data["code"], meal=data.get("meal"))
    except PassError as exc:
        return Response(
            {"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
        )
    return Response(_scan_payload(student, slot, checkin), status=status.HTTP_201_CREATED)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("crowd.view")])
def crowd(request):
    date = _date_param(request)
    slot = _meal_param(request)
    if slot:
        return Response(_meal_payload(crowd_forecast(date, slot)))
    return Response(
        {
            "date": date,
            "meals": [_meal_payload(crowd_forecast(date, s)) for s in MealSlot.objects.all()],
        }
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("leaderboard.view")])
def leaderboard(request):
    days = _int_param(request, "days", 30, 1, 120)
    return Response(
        {
            "days": days,
            "rows": hostel_leaderboard(today(), days=days),
            "assumptions": assumptions(),
        }
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("rewards.own")])
def rewards_me(request):
    summary = student_summary(request.user, today())
    recent = list(
        RewardEvent.objects.filter(student=request.user)
        .select_related("badge")
        .order_by("-created_at")[:20]
        .values("points", "reason", "date", "detail", "badge__name", "badge__icon")
    )
    return Response(
        {
            "summary": _rewards_payload(summary),
            "recent": recent,
            "assumptions": assumptions(),
        }
    )


# ---------------------------------------------------------------------------
# Manager endpoints
# ---------------------------------------------------------------------------
@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("plan.view")])
def manager_preparation_plan(request):
    date = _date_param(request)
    rows, totals = _plan_rows(date)
    paginator = StandardPagination()
    page = paginator.paginate_queryset(rows, request, view=manager_preparation_plan)
    response = paginator.get_paginated_response(page)
    response.data["date"] = date
    response.data["summary"] = totals
    response.data["assumptions"] = assumptions()
    return response


@extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("plan.approve")])
def approve_plan(request, pk: int):
    plan = get_object_or_404(PreparationPlan, pk=pk)
    plan.status = PreparationPlan.Status.APPROVED
    plan.approved_by = request.user
    plan.approved_at = dt.datetime.now(tz=dt.timezone.utc)
    plan.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    PlanDecisionLog.objects.create(
        plan=plan,
        action=PlanDecisionLog.Action.APPROVE,
        reason=request.data.get("reason", "")[:1000],
        actor=request.user,
    )
    return Response(
        {
            "id": plan.pk,
            "status": plan.status,
            "approved_by": plan.approved_by.username if plan.approved_by else None,
            "approved_at": plan.approved_at,
        }
    )


@extend_schema(request=OverrideSerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("plan.override")])
def override_plan(request, pk: int):
    plan = get_object_or_404(PreparationPlan, pk=pk)
    serializer = OverrideSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    reason = serializer.validated_data["reason"].strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required for an override."})
    if "recommended_kg" in serializer.validated_data:
        plan.recommended_kg = serializer.validated_data["recommended_kg"]
    plan.status = PreparationPlan.Status.OVERRIDDEN
    plan.override_reason = reason
    plan.approved_by = request.user
    plan.approved_at = dt.datetime.now(tz=dt.timezone.utc)
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
    return Response(
        {
            "id": plan.pk,
            "recommended_kg": float(plan.recommended_kg),
            "status": plan.status,
            "override_reason": plan.override_reason,
        }
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("live.view")])
def manager_live(request):
    date = _date_param(request)
    slot = _meal_param(request)
    if slot:
        return Response(_meal_payload(live_meal_status(date, slot)))
    return Response(
        {
            "date": date,
            "meals": [_meal_payload(live_meal_status(date, s)) for s in MealSlot.objects.all()],
        }
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("waste.view")])
def manager_waste(request):
    days = _int_param(request, "days", 30, 1, 365)
    return Response(waste_analytics(days))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("waste.view")])
def manager_waste_logs(request):
    days = _int_param(request, "days", 30, 1, 365)
    start = today() - dt.timedelta(days=days - 1)
    qs = WasteLog.objects.filter(date__gte=start).select_related("meal", "dish").order_by("-date")
    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request, view=manager_waste_logs)
    return paginator.get_paginated_response(WasteLogSerializer(page, many=True).data)


@extend_schema(request=WasteLogWriteSerializer, responses={201: WasteLogSerializer})
@api_view(["POST"])
@permission_classes([HasPerm("waste.write")])
def manager_waste_record(request):
    serializer = WasteLogWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        log = record_waste(
            data["date"],
            data["meal"],
            data["dish"],
            data["prepared_kg"],
            data["consumed_kg"],
            data["wasted_kg"],
            leftover_reusable=data["leftover_reusable"],
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(WasteLogSerializer(log).data, status=status.HTTP_201_CREATED)


@extend_schema(request=CloseDaySerializer, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("service.close")])
def manager_close_day(request):
    serializer = CloseDaySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        summary = close_service_day(
            serializer.validated_data["date"],
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {
            "date": summary["date"],
            "meals": summary["meals"],
            "checkins": summary["checkins"],
            "positive_intents": summary["positive_intents"],
            "wasted_kg": float(summary["wasted_kg"]),
        }
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("impact.view")])
def manager_impact(request):
    days = _int_param(request, "days", 30, 1, 365)
    return Response(impact_summary(days))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("menu.manage")])
def manager_menu_insights(request):
    days = _int_param(request, "days", 30, 1, 365)
    return Response(menu_insights(days))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("plan.view")])
def manager_peak(request):
    date = _date_param(request)
    slot = _meal_param(request) or MealSlot.objects.first()
    if not slot:
        return Response({"detail": "No meal slots configured."}, status=404)
    return Response(_meal_payload(peak_analysis(date, slot)))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([HasPerm("model.manage")])
def manager_model_health(request):
    health = model_health()
    run = health["run"]
    data = {"empty": health["empty"], "assumptions": health["assumptions"]}
    if run:
        data.update(
            {
                "version": run.version,
                "trained_at": run.trained_at,
                "participation_algorithm": run.participation_algorithm,
                "dish_algorithm": run.dish_algorithm,
                "participation_mae": run.participation_mae,
                "participation_mape": run.participation_mape,
                "participation_baseline_mae": run.participation_baseline_mae,
                "participation_baseline_mape": run.participation_baseline_mape,
                "participation_improvement_pct": round(run.participation_improvement_pct, 1),
                "dish_mae_kg": run.dish_mae_kg,
                "dish_mape": run.dish_mape,
                "dish_baseline_mae_kg": run.dish_baseline_mae_kg,
                "dish_baseline_mape": run.dish_baseline_mape,
                "dish_improvement_pct": round(run.dish_improvement_pct, 1),
                "train_rows": run.train_rows,
                "validation_rows": run.validation_rows,
                "validation_start": run.validation_start,
                "validation_end": run.validation_end,
                "metrics": run.metrics,
            }
        )
    return Response(data)


@extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("model.manage")])
def manager_retrain(request):
    try:
        run = train_and_store(
            validation_days=int(request.data.get("validation_days", 21)),
            generate=True,
            days=int(request.data.get("days", 8)),
        )
    except (InsufficientData, ValueError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {
            "version": run.version,
            "participation_mae": run.participation_mae,
            "participation_improvement_pct": round(run.participation_improvement_pct, 1),
            "dish_mae_kg": run.dish_mae_kg,
            "dish_improvement_pct": round(run.dish_improvement_pct, 1),
        }
    )


@extend_schema(request=ImportSerializer, responses={201: OpenApiTypes.OBJECT})
@api_view(["POST"])
@permission_classes([HasPerm("import.csv")])
def manager_import_csv(request):
    kind = request.data.get("kind")
    upload = request.FILES.get("file")
    if kind not in {"participation", "menus", "prepared", "wastage"}:
        raise ValidationError({"kind": "Use participation, menus, prepared or wastage."})
    if not upload:
        raise ValidationError({"file": "Attach a CSV file."})
    if upload.size > MAX_CSV_BYTES:
        raise ValidationError(
            {"file": f"That file is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB."}
        )
    try:
        raw = upload.read()
    except Exception:
        return Response({"detail": "Could not read the uploaded file."}, status=400)
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    try:
        result = import_csv_file(
            kind=kind,
            filename=upload.name,
            content=content,
            user=request.user,
            dry_run=str(request.data.get("dry_run", "")).lower() in {"1", "true", "yes"},
            allow_partial=str(request.data.get("allow_partial", "")).lower()
            in {"1", "true", "yes"},
        )
    except CsvImportError as exc:
        return Response({"detail": "CSV import failed.", "errors": exc.errors}, status=400)
    return Response(result.as_dict(), status=201)


@extend_schema(responses={(200, "text/csv"): OpenApiTypes.STR})
@api_view(["GET"])
@permission_classes([HasPerm("plan.view")])
def manager_export_plan(request):
    date = _date_param(request)
    rows, _totals = _plan_rows(date)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "meal",
            "dish",
            "recommended_kg",
            "baseline_kg",
            "forecast_lower_kg",
            "forecast_upper_kg",
            "predicted_participants",
            "status",
            "override_reason",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["date"],
                row["meal"],
                row["dish"],
                row["recommended_kg"],
                row["baseline_kg"],
                row["forecast"]["lower_bound_kg"],
                row["forecast"]["upper_bound_kg"],
                row["forecast"]["predicted_participation"],
                row["status"],
                row["override_reason"],
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="messlab-plan-{date}.csv"'
    return response


# ---------------------------------------------------------------------------
# Menu management
# ---------------------------------------------------------------------------
@extend_schema(
    methods=["GET"], responses={200: DishWriteSerializer(many=True)}
)
@extend_schema(methods=["POST"], request=DishWriteSerializer, responses={201: DishWriteSerializer})
@api_view(["GET", "POST"])
@permission_classes([HasPerm("menu.manage")])
def manager_dishes(request):
    if request.method == "GET":
        qs = Dish.objects.all()
        active = request.query_params.get("active")
        if active in {"0", "1"}:
            qs = qs.filter(is_active=active == "1")
        query = (request.query_params.get("q") or "").strip()
        if query:
            qs = qs.filter(name__icontains=query)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(
            DishWriteSerializer(page, many=True).data
        )

    serializer = DishWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        dish = save_dish(
            serializer.validated_data,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except MenuError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        DishWriteSerializer(dish).data, status=status.HTTP_201_CREATED
    )


@extend_schema(
    methods=["GET"], responses={200: DishWriteSerializer}
)
@extend_schema(methods=["PUT", "PATCH"], request=DishWriteSerializer, responses={200: DishWriteSerializer})
@extend_schema(methods=["DELETE"], responses={204: None})
@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([HasPerm("menu.manage")])
def manager_dish_detail(request, pk: int):
    dish = get_object_or_404(Dish, pk=pk)
    if request.method == "GET":
        return Response(DishWriteSerializer(dish).data)
    if request.method == "DELETE":
        try:
            delete_dish(
                dish,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except MenuError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = DishWriteSerializer(
        dish, data=request.data, partial=request.method == "PATCH"
    )
    serializer.is_valid(raise_exception=True)
    try:
        dish = save_dish(
            serializer.validated_data,
            instance=dish,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except MenuError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(DishWriteSerializer(dish).data)


@extend_schema(
    methods=["GET"], responses={200: DailyMenuSerializer(many=True)}
)
@extend_schema(methods=["POST"], request=DailyMenuWriteSerializer, responses={201: DailyMenuSerializer})
@api_view(["GET", "POST"])
@permission_classes([HasPerm("menu.manage")])
def manager_menus(request):
    if request.method == "GET":
        meal_name = request.query_params.get("meal") or ""
        meal = MealSlot.objects.filter(name=meal_name).first() if meal_name else None
        qs = menu_queryset(
            start=_optional_date_param(request, "start"),
            end=_optional_date_param(request, "end"),
            meal=meal,
            query=(request.query_params.get("q") or "").strip(),
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(
            DailyMenuSerializer(page, many=True).data
        )

    serializer = DailyMenuWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        menu = save_daily_menu(
            date=data["date"],
            meal=data["meal"],
            items=[
                (item["dish"], item["planned_quantity_kg"], item.get("is_special", False))
                for item in data["items"]
            ],
            details={
                item["dish"].pk: {
                    "serving_category": item.get(
                        "serving_category", MenuItem.ServingCategory.UNLIMITED
                    ),
                    "portion_grams": item.get("portion_grams"),
                    "frequency_per_month": item.get("frequency_per_month"),
                }
                for item in data["items"]
            },
            notes=data.get("notes", ""),
            is_special=data.get("is_special", False),
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except MenuError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        DailyMenuSerializer(menu).data, status=status.HTTP_201_CREATED
    )


@extend_schema(methods=["GET"], responses={200: DailyMenuSerializer})
@extend_schema(methods=["PUT", "PATCH"], request=DailyMenuWriteSerializer, responses={200: DailyMenuSerializer})
@extend_schema(methods=["DELETE"], responses={204: None})
@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([HasPerm("menu.manage")])
def manager_menu_detail(request, pk: int):
    menu = get_object_or_404(
        DailyMenu.objects.select_related("meal").prefetch_related("items__dish"), pk=pk
    )
    if request.method == "GET":
        return Response(DailyMenuSerializer(menu).data)
    if request.method == "DELETE":
        delete_menu(
            menu,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    payload = {
        "date": menu.date,
        "meal": menu.meal.name,
        "notes": menu.notes,
        "is_special": menu.is_special,
        "items": [
            {
                "dish": item.dish_id,
                "planned_quantity_kg": item.planned_quantity_kg,
                "is_special": item.is_special,
                "serving_category": item.serving_category,
                "portion_grams": item.portion_grams,
                "frequency_per_month": item.frequency_per_month,
            }
            for item in menu.items.all()
        ],
    }
    payload.update(request.data)
    serializer = DailyMenuWriteSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        menu = save_daily_menu(
            date=data["date"],
            meal=data["meal"],
            items=[
                (item["dish"], item["planned_quantity_kg"], item.get("is_special", False))
                for item in data["items"]
            ],
            details={
                item["dish"].pk: {
                    "serving_category": item.get(
                        "serving_category", MenuItem.ServingCategory.UNLIMITED
                    ),
                    "portion_grams": item.get("portion_grams"),
                    "frequency_per_month": item.get("frequency_per_month"),
                }
                for item in data["items"]
            },
            notes=data.get("notes", ""),
            is_special=data.get("is_special", False),
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except MenuError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(DailyMenuSerializer(menu).data)


# ---------------------------------------------------------------------------
# Account administration
# ---------------------------------------------------------------------------
def _user_queryset(request):
    qs = User.objects.select_related("role_profile", "profile").order_by("username")
    if role_of(request.user) == User.Role.SUPERVISOR:
        qs = qs.filter(
            role_profile__role__in=[User.Role.WORKER, User.Role.CANTEEN_STAFF]
        )
    role_filter = request.query_params.get("role", "")
    if role_filter in User.Role.values:
        qs = qs.filter(role_profile__role=role_filter)
    status_filter = request.query_params.get("status", "")
    if status_filter == "active":
        qs = qs.filter(is_active=True)
    elif status_filter == "inactive":
        qs = qs.filter(is_active=False)
    query = (request.query_params.get("q") or "").strip()
    if query:
        qs = qs.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
            | Q(profile__student_code__icontains=query)
        )
    return qs


@extend_schema(responses={200: AdminUserSerializer(many=True)})
@api_view(["GET"])
@permission_classes([HasPerm("users.view")])
def manager_users(request):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(_user_queryset(request), request)
    show_pii = user_can(request.user, "users.role")
    serializer = AdminUserSerializer(
        page, many=True, context={"show_pii": show_pii}
    )
    return paginator.get_paginated_response(serializer.data)


@extend_schema(responses={200: AdminUserSerializer})
@api_view(["GET"])
@permission_classes([HasPerm("users.view")])
def manager_user_detail(request, pk: int):
    target = get_object_or_404(_user_queryset(request), pk=pk)
    show_pii = user_can(request.user, "users.role")
    return Response(
        AdminUserSerializer(target, context={"show_pii": show_pii}).data
    )


@extend_schema(request=RoleUpdateSerializer, responses={200: AdminUserSerializer})
@api_view(["POST"])
@permission_classes([HasPerm("users.role")])
def manager_user_role(request, pk: int):
    target = get_object_or_404(User, pk=pk)
    serializer = RoleUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        change_role(
            request.user, target, serializer.validated_data["role"], request
        )
    except AccountAdminError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    target.refresh_from_db()
    return Response(
        AdminUserSerializer(target, context={"show_pii": True}).data
    )


@extend_schema(request=ActiveUpdateSerializer, responses={200: AdminUserSerializer})
@api_view(["POST"])
@permission_classes([HasPerm("users.manage")])
def manager_user_active(request, pk: int):
    target = get_object_or_404(User, pk=pk)
    serializer = ActiveUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        set_active(
            request.user, target, serializer.validated_data["active"], request
        )
    except AccountAdminError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    target.refresh_from_db()
    return Response(
        AdminUserSerializer(target, context={"show_pii": True}).data
    )


@extend_schema(responses={200: AccountAuditSerializer(many=True)})
@api_view(["GET"])
@permission_classes([HasPerm("audit.view")])
def manager_audit(request):
    qs = AccountAudit.objects.select_related("actor__profile", "target__profile")
    action_filter = request.query_params.get("action", "")
    if action_filter in AccountAudit.Action.values:
        qs = qs.filter(action=action_filter)
    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response(
        AccountAuditSerializer(page, many=True).data
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def privacy_export(request):
    return Response(export_person(request.user))


@extend_schema(request=OpenApiTypes.OBJECT, responses={204: None})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def privacy_delete(request):
    if str(request.data.get("confirm", "")).upper() != "DELETE":
        return Response(
            {"detail": "Send {\"confirm\": \"DELETE\"} to delete the account."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    delete_person(request.user)
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok"})
