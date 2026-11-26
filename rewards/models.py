"""Lightweight green-points gamification. No ratings, no complaints."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Badge(models.Model):
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240)
    icon = models.CharField(max_length=8, default="🌱")
    points = models.PositiveSmallIntegerField(default=10)
    threshold = models.PositiveIntegerField(
        default=0, help_text="Optional count threshold used to explain the badge."
    )

    class Meta:
        ordering = ["threshold", "name"]

    def __str__(self) -> str:
        return self.name


class RewardEvent(models.Model):
    class Reason(models.TextChoices):
        INTENT_HONOURED = "intent_honoured", "Intent honoured"
        SKIP_NOTICE = "skip_notice", "Skipped with notice"
        CLEAN_PLATE = "clean_plate", "Clean plate"
        STREAK = "streak", "Streak"
        BADGE = "badge", "Badge earned"
        CHECKIN = "checkin", "Meal check-in"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reward_events"
    )
    points = models.IntegerField(default=0)
    reason = models.CharField(max_length=24, choices=Reason.choices)
    date = models.DateField(db_index=True)
    badge = models.ForeignKey(Badge, on_delete=models.SET_NULL, null=True, blank=True)
    detail = models.CharField(max_length=240, blank=True)
    unique_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student", "-date"], name="reward_student_date_idx"),
            models.Index(fields=["date", "reason"], name="reward_date_reason_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.reason} {self.points:+d}"
