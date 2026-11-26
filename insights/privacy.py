"""Personal-data export, deletion and retention helpers."""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from accounts.models import AccountAudit, LoginEvent, OTPCode, User
from dining.models import CheckIn, MealFeedback, MealIntent
from rewards.models import RewardEvent


def export_person(user: User) -> dict:
    """Everything MessLab holds that is directly tied to one account."""
    profile = getattr(user, "role_profile", None)
    student = getattr(user, "profile", None)
    return {
        "generated_at": timezone.now().isoformat(),
        "account": {
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.effective_role,
            "is_active": user.is_active,
            "date_joined": user.date_joined.isoformat(),
        },
        "profile": {
            "phone": getattr(profile, "phone", ""),
            "hostel": getattr(profile, "hostel", ""),
            "student_code": getattr(student, "student_code", ""),
            "diet": getattr(student, "diet", ""),
            "allergies": getattr(student, "allergies", ""),
            "consent_at": (
                profile.data_consent_at.isoformat()
                if profile and profile.data_consent_at
                else None
            ),
            "consent_version": getattr(profile, "data_consent_version", ""),
        },
        "meal_intents": [
            {
                "date": row.date.isoformat(),
                "meal": row.meal.name,
                "status": row.status,
                "source": row.source,
            }
            for row in MealIntent.objects.filter(student=user).select_related("meal")
        ],
        "check_ins": [
            {
                "date": row.date.isoformat(),
                "meal": row.meal.name,
                "timestamp": row.timestamp.isoformat(),
                "source": row.source,
                "clean_plate": row.clean_plate,
            }
            for row in CheckIn.objects.filter(student=user).select_related("meal")
        ],
        "menu_feedback": [
            {
                "date": row.date.isoformat(),
                "meal": row.meal.name,
                "response": row.response,
                "suggestion": row.suggestion,
            }
            for row in MealFeedback.objects.filter(student=user).select_related("meal")
        ],
        "reward_events": [
            {
                "date": row.date.isoformat(),
                "points": row.points,
                "reason": row.reason,
            }
            for row in RewardEvent.objects.filter(student=user)
        ],
    }


def delete_person(user: User) -> None:
    """Delete the account and every row that cascades from it.

    Aggregate ``MealService`` rows remain, but they contain counts only and no
    account identifier.
    """
    AccountAudit.objects.create(
        actor=None,
        action=AccountAudit.Action.DEACTIVATE,
        detail={"self_deleted": True, "user_id": user.pk},
    )
    user.delete()


def purge_older_than(days: int, *, dry_run: bool = False) -> dict:
    """Remove short-lived personal traces after the retention window."""
    cutoff = timezone.now() - dt.timedelta(days=days)
    counts = {
        "login_events": LoginEvent.objects.filter(at__lt=cutoff).count(),
        "otp_codes": OTPCode.objects.filter(created_at__lt=cutoff).count(),
        "account_audit": AccountAudit.objects.filter(at__lt=cutoff).count(),
    }
    if not dry_run:
        LoginEvent.objects.filter(at__lt=cutoff).delete()
        OTPCode.objects.filter(created_at__lt=cutoff).delete()
        AccountAudit.objects.filter(at__lt=cutoff).delete()
    return {**counts, "cutoff": cutoff.isoformat(), "dry_run": dry_run}
