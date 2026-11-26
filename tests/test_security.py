"""Phase 7: cross-cutting security hardening checks."""
from __future__ import annotations

from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from dining.importers import CsvImportError, import_csv_file
from dining.passes import PassError, read_pass
from tests.factories import BASE_DATE, make_slots, make_student, make_user, set_today


class HeaderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("header-student", hostel="H1")

    def test_pages_carry_hardening_headers(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:student-home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertIn("camera=(self)", response["Permissions-Policy"])
        self.assertIn("microphone=()", response["Permissions-Policy"])

    def test_api_responses_carry_the_same_headers(self):
        self.client.force_login(self.student)
        response = self.client.get("/api/v1/meals/today/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")


class CsrfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_slots()
        set_today(BASE_DATE)
        cls.canteen = make_user("csrf-canteen", User.Role.CANTEEN_STAFF)
        cls.student = make_student("csrf-student", hostel="H1")

    def test_scanner_post_without_a_csrf_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.canteen)
        response = client.post(
            reverse("web:staff-scan"),
            {"code": self.student.profile.student_code, "meal": "lunch"},
        )
        self.assertEqual(response.status_code, 403)


class TokenClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("claim-student", hostel="H1")

    def _bearer(self, user, **claims) -> str:
        refresh = RefreshToken.for_user(user)
        refresh["token_version"] = user.token_version
        for key, value in claims.items():
            refresh[key] = value
        return str(refresh.access_token)

    def test_a_forged_role_claim_does_not_grant_manager_access(self):
        token = self._bearer(self.student, role=User.Role.MANAGER)
        response = self.client.get(
            "/api/v1/manager/preparation-plan/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_disabled_account_cannot_keep_using_an_old_token(self):
        token = self._bearer(self.student)
        User.objects.filter(pk=self.student.pk).update(is_active=False)
        response = self.client.get(
            "/api/v1/meals/today/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, 401)

    def test_an_old_token_version_is_rejected(self):
        token = self._bearer(self.student)
        User.objects.filter(pk=self.student.pk).update(
            token_version=self.student.token_version + 1
        )
        response = self.client.get(
            "/api/v1/meals/today/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, 401)


class InputLimitTests(TestCase):
    def test_csv_body_size_is_bounded(self):
        content = "date,meal,dish,planned_quantity_kg\n2026-03-02,lunch,Dal,10\n"
        with mock.patch("dining.importers.MAX_CSV_BYTES", 10):
            with self.assertRaises(CsvImportError) as caught:
                import_csv_file("menus", "big.csv", content)
        self.assertIn("larger than", " ".join(caught.exception.errors))

    def test_csv_row_count_is_bounded(self):
        content = (
            "date,meal,dish,planned_quantity_kg\n"
            "2026-03-02,lunch,Dal,10\n"
            "2026-03-02,lunch,Rice,8\n"
        )
        with mock.patch("dining.importers.MAX_CSV_ROWS", 1):
            with self.assertRaises(CsvImportError) as caught:
                import_csv_file("menus", "long.csv", content)
        self.assertIn("more than", " ".join(caught.exception.errors))

    def test_oversized_pass_tokens_are_rejected_before_signing(self):
        with self.assertRaises(PassError) as caught:
            read_pass("x" * 3000, date=BASE_DATE)
        self.assertEqual(caught.exception.code, "invalid")
