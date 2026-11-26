"""Green points, streaks, badges and leaderboard behaviour."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.test import TestCase

from dining.models import CheckIn
from dining.services import check_in_student, set_intent
from rewards.models import Badge, RewardEvent
from rewards.services import (
    award_day_rewards,
    current_streak,
    hostel_leaderboard,
    student_summary,
)
from tests.factories import (
    BASE_DATE,
    make_checkin,
    make_dish,
    make_intent,
    make_menu,
    make_service,
    make_slots,
    make_student,
    make_waste,
    set_today,
)


class RewardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Masala Dosa")
        cls.student = make_student("reward-student", hostel="H1")
        set_today(BASE_DATE)
        make_menu(BASE_DATE, cls.slots["lunch"], [(cls.dish, 20)])
        make_intent(cls.student, BASE_DATE, cls.slots["lunch"])

    def test_checkin_awards_points_once(self):
        check_in_student(
            self.student,
            BASE_DATE,
            self.slots["lunch"],
            dish_ids=[self.dish.pk],
            clean_plate=True,
        )
        summary = student_summary(self.student, BASE_DATE)
        self.assertEqual(summary["points"], 19)  # 3 check-in + 6 clean plate + 10 intent honoured
        self.assertEqual(summary["clean_plates"], 1)
        self.assertEqual(summary["honoured"], 1)
        self.assertEqual(CheckIn.objects.count(), 1)
        with self.assertRaises(ValueError):
            check_in_student(self.student, BASE_DATE, self.slots["lunch"])

    def test_skip_notice_awards_early_intent_points(self):
        with patch(
            "django.utils.timezone.now",
            return_value=dt.datetime(2026, 3, 2, 5, 0, tzinfo=dt.timezone.utc),
        ):
            set_intent(
                self.student, BASE_DATE, self.slots["lunch"], "NO", source="one_tap"
            )
        event = RewardEvent.objects.get(
            student=self.student, reason=RewardEvent.Reason.SKIP_NOTICE
        )
        self.assertEqual(event.points, 8)
        summary = student_summary(self.student, BASE_DATE)
        self.assertEqual(summary["skips"], 1)

    def test_three_day_streak_creates_badge(self):
        Badge.objects.create(
            code="streak_3", name="Three in a row", description="3-day streak", threshold=3
        )
        for offset in range(3):
            make_checkin(
                self.student,
                BASE_DATE - dt.timedelta(days=offset),
                self.slots["lunch"],
            )
        streak = current_streak(self.student, BASE_DATE)
        self.assertEqual(streak, 3)
        self.assertTrue(
            RewardEvent.objects.filter(
                student=self.student, reason=RewardEvent.Reason.BADGE
            ).exists()
        )

    def test_hostel_leaderboard_aggregates_points(self):
        other = make_student("reward-student-2", hostel="H2")
        RewardEvent.objects.create(
            student=self.student,
            date=BASE_DATE,
            reason=RewardEvent.Reason.CHECKIN,
            points=20,
            unique_key="leaderboard-1",
        )
        RewardEvent.objects.create(
            student=other,
            date=BASE_DATE,
            reason=RewardEvent.Reason.CHECKIN,
            points=5,
            unique_key="leaderboard-2",
        )
        make_service(BASE_DATE, self.slots["lunch"], 10)
        make_waste(BASE_DATE, self.slots["lunch"], self.dish, 10, 8, 2)
        rows = hostel_leaderboard(BASE_DATE, days=30)
        self.assertEqual(rows[0]["hostel"], "H1")
        self.assertEqual(rows[0]["points"], 20)
        self.assertGreaterEqual(rows[0]["estimated_kg_saved"], 0)

    def test_bulk_day_rewards_are_idempotent(self):
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        first = award_day_rewards(BASE_DATE)
        count_after_first = RewardEvent.objects.filter(student=self.student).count()
        second = award_day_rewards(BASE_DATE)
        count_after_second = RewardEvent.objects.filter(student=self.student).count()
        self.assertGreater(first, 0)
        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(
            RewardEvent.objects.filter(
                student=self.student, reason=RewardEvent.Reason.CHECKIN
            ).count(),
            1,
        )
        self.assertGreaterEqual(second, 0)
