"""Phase 5: signed QR passes, camera/manual check-in and the scanner API."""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.conf import settings
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.authentication import bump_token_version
from accounts.models import User
from dining.models import CheckIn
from dining.passes import (
    PASS_SALT,
    PassError,
    issue_pass,
    manual_checkin,
    open_meal,
    pass_svg,
    read_pass,
    redeem_pass,
)
from tests.factories import BASE_DATE, make_slots, make_student, make_user, set_today


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return timezone.make_aware(
        dt.datetime.combine(day, dt.time(hour, minute)),
        timezone.get_current_timezone(),
    )


class PassTokenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("pass-student", hostel="H1")

    def test_round_trip_returns_the_student_and_service_day(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        user, day = read_pass(token, date=BASE_DATE, meal=self.slots["lunch"])
        self.assertEqual(user.pk, self.student.pk)
        self.assertEqual(day, BASE_DATE)

    def test_the_token_carries_no_identity_text(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        payload = signing.loads(token, salt=PASS_SALT)
        self.assertEqual(set(payload), {"u", "d", "v", "m"})
        self.assertEqual(payload["m"], "lunch")
        self.assertNotIn(self.student.username, token)
        self.assertNotIn(self.student.email, token)

    def test_tampering_is_rejected(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        replacement = "A" if token[10] != "A" else "B"
        tampered = token[:10] + replacement + token[11:]
        with self.assertRaises(PassError) as caught:
            read_pass(tampered, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "invalid")

    def test_expired_pass_is_rejected(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        expired = {**settings.MESSLAB, "QR_TTL_SECONDS": -1}
        with override_settings(MESSLAB=expired):
            with self.assertRaises(PassError) as caught:
                read_pass(token, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "expired")

    def test_a_pass_for_another_day_is_rejected(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        with self.assertRaises(PassError) as caught:
            read_pass(token, date=BASE_DATE + dt.timedelta(days=1))
        self.assertEqual(caught.exception.code, "wrong_day")

    def test_a_pass_bound_to_another_meal_is_rejected(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["breakfast"])
        with self.assertRaises(PassError) as caught:
            read_pass(token, date=BASE_DATE, meal=self.slots["lunch"])
        self.assertEqual(caught.exception.code, "wrong_meal")

    def test_bumping_the_token_version_invalidates_the_pass(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        bump_token_version(self.student)
        with self.assertRaises(PassError) as caught:
            read_pass(token, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "stale")

    def test_a_deactivated_account_cannot_use_a_pass(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        User.objects.filter(pk=self.student.pk).update(is_active=False)
        with self.assertRaises(PassError) as caught:
            read_pass(token, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "inactive")

    def test_empty_pass_is_rejected(self):
        with self.assertRaises(PassError) as caught:
            read_pass("", date=BASE_DATE)
        self.assertEqual(caught.exception.code, "empty")


class MealWindowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)

    def test_open_meal_finds_the_serving_window(self):
        self.assertEqual(open_meal(BASE_DATE, now=at(BASE_DATE, 13)).pk, self.slots["lunch"].pk)

    def test_open_meal_allows_the_counter_grace_period(self):
        self.assertEqual(
            open_meal(BASE_DATE, now=at(BASE_DATE, 11, 45)).pk, self.slots["lunch"].pk
        )
        self.assertEqual(
            open_meal(BASE_DATE, now=at(BASE_DATE, 14, 55)).pk, self.slots["lunch"].pk
        )

    def test_open_meal_is_none_between_services(self):
        self.assertIsNone(open_meal(BASE_DATE, now=at(BASE_DATE, 15, 30)))


class RedemptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("redeem-student", hostel="H1")
        cls.canteen = make_user("redeem-canteen", User.Role.CANTEEN_STAFF)

    def test_redeeming_a_pass_writes_a_qr_checkin(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        user, slot, checkin = redeem_pass(
            token, meal=self.slots["lunch"], date=BASE_DATE
        )
        self.assertEqual(user.pk, self.student.pk)
        self.assertEqual(slot.pk, self.slots["lunch"].pk)
        self.assertEqual(checkin.source, CheckIn.Source.QR)
        self.assertTrue(
            CheckIn.objects.filter(
                student=self.student, date=BASE_DATE, meal=self.slots["lunch"]
            ).exists()
        )

    def test_redeeming_without_an_override_uses_the_open_meal(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        with mock.patch(
            "dining.passes.timezone.now", return_value=at(BASE_DATE, 13)
        ):
            _user, slot, _checkin = redeem_pass(token, date=BASE_DATE)
        self.assertEqual(slot.pk, self.slots["lunch"].pk)

    def test_redeeming_outside_service_hours_is_rejected(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        with mock.patch(
            "dining.passes.timezone.now", return_value=at(BASE_DATE, 15, 30)
        ):
            with self.assertRaises(PassError) as caught:
                redeem_pass(token, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "closed")
        self.assertFalse(CheckIn.objects.exists())

    def test_a_second_scan_is_a_duplicate(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        redeem_pass(token, meal=self.slots["lunch"], date=BASE_DATE)
        with self.assertRaises(PassError) as caught:
            redeem_pass(token, meal=self.slots["lunch"], date=BASE_DATE)
        self.assertEqual(caught.exception.code, "duplicate")

    def test_manual_entry_by_student_code(self):
        user, slot, checkin = manual_checkin(
            self.student.profile.student_code.lower(),
            meal=self.slots["dinner"],
            date=BASE_DATE,
        )
        self.assertEqual(user.pk, self.student.pk)
        self.assertEqual(slot.pk, self.slots["dinner"].pk)
        self.assertEqual(checkin.source, CheckIn.Source.MANUAL)

    def test_unknown_or_inactive_codes_are_rejected(self):
        with self.assertRaises(PassError) as caught:
            manual_checkin("NOPE-000", meal=self.slots["lunch"], date=BASE_DATE)
        self.assertEqual(caught.exception.code, "unknown")

        User.objects.filter(pk=self.student.pk).update(is_active=False)
        with self.assertRaises(PassError) as caught:
            manual_checkin(
                self.student.profile.student_code,
                meal=self.slots["lunch"],
                date=BASE_DATE,
            )
        self.assertEqual(caught.exception.code, "unknown")

    def test_empty_manual_code_is_rejected(self):
        with self.assertRaises(PassError) as caught:
            manual_checkin("", meal=self.slots["lunch"], date=BASE_DATE)
        self.assertEqual(caught.exception.code, "empty")

    def test_the_svg_is_an_inline_document(self):
        svg = pass_svg(issue_pass(self.student, BASE_DATE))
        self.assertIn("<svg", svg)
        self.assertNotIn("<script", svg.lower())


class StudentPassWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("web-pass-student", hostel="H1")
        cls.manager = make_user("web-pass-manager", User.Role.MANAGER)

    def test_checkin_page_shows_the_signed_pass(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:student-checkin"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/student/pass/qr.svg")
        self.assertIn("pass_token", response.context)

    def test_qr_endpoint_returns_an_uncached_svg(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:student-pass-qr"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn(b"<svg", response.content)

    def test_qr_endpoint_is_denied_to_managers_and_anonymous_users(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("web:student-pass-qr")).status_code, 403)
        self.client.logout()
        response = self.client.get(reverse("web:student-pass-qr"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


class ScannerWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("scan-student", hostel="H1")
        cls.canteen = make_user("scan-canteen", User.Role.CANTEEN_STAFF)
        cls.worker = make_user("scan-worker", User.Role.WORKER)

    def test_scanner_page_has_camera_and_manual_form(self):
        self.client.force_login(self.canteen)
        response = self.client.get(reverse("web:staff-scan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="scan-video"')
        self.assertContains(response, "Manual check-in")
        self.assertContains(response, "data-scan-form")

    def test_workers_cannot_open_the_scanner(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("web:staff-scan")).status_code, 403)

    def test_scanning_a_valid_pass_checks_the_student_in(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        self.client.force_login(self.canteen)
        response = self.client.post(
            reverse("web:staff-scan"),
            {"pass_token": token, "meal": self.slots["lunch"].name},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checked in")
        checkin = CheckIn.objects.get(student=self.student, date=BASE_DATE)
        self.assertEqual(checkin.source, CheckIn.Source.QR)

    def test_a_bad_pass_shows_an_error_and_writes_nothing(self):
        self.client.force_login(self.canteen)
        response = self.client.post(
            reverse("web:staff-scan"),
            {"pass_token": "not-a-pass", "meal": self.slots["lunch"].name},
            follow=True,
        )
        self.assertContains(response, "not a valid MessLab pass")
        self.assertFalse(CheckIn.objects.exists())

    def test_manual_entry_checks_in_by_code(self):
        self.client.force_login(self.canteen)
        response = self.client.post(
            reverse("web:staff-scan"),
            {
                "code": self.student.profile.student_code,
                "meal": self.slots["dinner"].name,
            },
            follow=True,
        )
        self.assertContains(response, "Checked in")
        checkin = CheckIn.objects.get(student=self.student, date=BASE_DATE)
        self.assertEqual(checkin.source, CheckIn.Source.MANUAL)
        self.assertEqual(checkin.meal_id, self.slots["dinner"].pk)

    def test_unknown_code_shows_an_error(self):
        self.client.force_login(self.canteen)
        response = self.client.post(
            reverse("web:staff-scan"),
            {"code": "UNKNOWN-1", "meal": self.slots["lunch"].name},
            follow=True,
        )
        self.assertContains(response, "No active student matches that code")
        self.assertFalse(CheckIn.objects.exists())


class CheckinApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("api-pass-student", hostel="H1")
        cls.canteen = make_user("api-pass-canteen", User.Role.CANTEEN_STAFF)
        cls.manager = make_user("api-pass-manager", User.Role.MANAGER)

    def test_pass_endpoint_returns_a_working_token_and_svg(self):
        self.client.force_login(self.student)
        response = self.client.get("/api/v1/checkin/pass/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("<svg", payload["qr_svg"])
        self.assertIn(payload["meal"], {"breakfast", "lunch", "snacks", "dinner"})
        user, day = read_pass(payload["token"], date=BASE_DATE)
        self.assertEqual(user.pk, self.student.pk)
        self.assertEqual(day, BASE_DATE)

    def test_managers_cannot_ask_for_a_student_pass(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get("/api/v1/checkin/pass/").status_code, 403)

    def test_canteen_scan_endpoint_redeems_a_pass(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        self.client.force_login(self.canteen)
        response = self.client.post(
            "/api/v1/staff/scan/",
            {"token": token, "meal": "lunch"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["student_code"], self.student.profile.student_code)
        self.assertEqual(payload["checkin"]["source"], CheckIn.Source.QR)
        self.assertEqual(payload["meal"], "lunch")

    def test_students_cannot_use_the_scan_endpoint(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        self.client.force_login(self.student)
        response = self.client.post(
            "/api/v1/staff/scan/",
            {"token": token, "meal": "lunch"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_scan_returns_the_reason_code(self):
        self.client.force_login(self.canteen)
        response = self.client.post(
            "/api/v1/staff/scan/",
            {"token": "broken", "meal": "lunch"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid")

    def test_duplicate_scan_is_a_400(self):
        token = issue_pass(self.student, BASE_DATE, self.slots["lunch"])
        self.client.force_login(self.canteen)
        first = self.client.post(
            "/api/v1/staff/scan/",
            {"token": token, "meal": "lunch"},
            content_type="application/json",
        )
        second = self.client.post(
            "/api/v1/staff/scan/",
            {"token": token, "meal": "lunch"},
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["code"], "duplicate")

    def test_manual_endpoint_uses_the_typed_code(self):
        self.client.force_login(self.canteen)
        response = self.client.post(
            "/api/v1/staff/check-in/manual/",
            {"code": self.student.profile.student_code, "meal": "snacks"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["checkin"]["source"], CheckIn.Source.MANUAL)
        self.assertEqual(response.json()["meal"], "snacks")

    def test_manual_endpoint_requires_the_manual_permission(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/api/v1/staff/check-in/manual/",
            {"code": self.student.profile.student_code, "meal": "snacks"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
