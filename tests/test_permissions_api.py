"""Role boundaries and API contract tests."""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

AUTH_SETTINGS = {
    **settings.MESSLAB,
    "ALLOW_PASSWORD_LOGIN": True,
}

from dining.models import PreparationPlan
from tests.factories import (
    BASE_DATE,
    latest_otp_code,
    make_dish,
    make_forecast,
    make_manager,
    make_menu,
    make_plan,
    make_slots,
    make_student,
    set_today,
)


class ApiAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Paneer Butter Masala", popularity=0.8)
        cls.student = make_student("api-student", password="student-pass")
        cls.manager = make_manager("api-manager", password="manager-pass")
        set_today(BASE_DATE)
        make_menu(BASE_DATE, cls.slots["lunch"], [(cls.dish, 20)])
        make_forecast(BASE_DATE, cls.slots["lunch"], participation=120, demand=20)
        make_forecast(
            BASE_DATE, cls.slots["lunch"], dish=cls.dish, participation=120, demand=20
        )
        cls.plan = make_plan(BASE_DATE, cls.slots["lunch"], cls.dish, 16, 20)

    def test_anonymous_cannot_read_student_data(self):
        response = self.client.get("/api/v1/meals/today/")
        self.assertIn(response.status_code, (401, 403))

    def test_student_cannot_use_manager_endpoint(self):
        self.client.force_login(self.student)
        response = self.client.get("/api/v1/manager/preparation-plan/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("plan.view", response.json()["detail"].lower())

    def test_manager_cannot_use_student_endpoint(self):
        self.client.force_login(self.manager)
        response = self.client.get("/api/v1/my-plan/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("plan.weekly", response.json()["detail"].lower())

    def test_student_today_payload_is_json_serializable_and_shaped(self):
        self.client.force_login(self.student)
        response = self.client.get("/api/v1/meals/today/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("meals", payload)
        lunch = next(row for row in payload["meals"] if row["meal"]["name"] == "lunch")
        self.assertIsInstance(lunch["meal"], dict)
        self.assertIsInstance(lunch["crowd"], dict)
        self.assertIsInstance(lunch["intent"], dict)
        self.assertIn("status", lunch["intent"])
        self.assertEqual(len(lunch["menu"]["items"]), 1)
        self.assertEqual(lunch["menu"]["items"][0]["name"], self.dish.name)

    def test_manager_plan_pagination_summary_and_peak(self):
        self.client.force_login(self.manager)
        response = self.client.get("/api/v1/manager/preparation-plan/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["summary"]["dishes"], 1)
        self.assertEqual(payload["summary"]["recommended_kg"], 16.0)
        self.assertEqual(payload["summary"]["saved_kg"], 4.0)

        peak = self.client.get("/api/v1/manager/peak/?meal=lunch")
        self.assertEqual(peak.status_code, 200)
        self.assertIsInstance(peak.json()["meal"], dict)
        self.assertEqual(len(peak.json()["heatmap"]), 7)

    def test_manager_export_is_csv(self):
        self.client.force_login(self.manager)
        response = self.client.get("/api/v1/manager/export/plan.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode()
        self.assertIn("recommended_kg", body)
        self.assertIn(self.dish.name, body)

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_api_otp_login_returns_jwt_and_user(self):
        email = self.student.email
        requested = self.client.post(
            "/api/v1/auth/otp/request/",
            data={"email": email},
            content_type="application/json",
        )
        self.assertEqual(requested.status_code, 200)
        self.assertNotIn("dev_hint", requested.json())
        code = latest_otp_code()
        self.assertRegex(code, r"^\d{6}$")

        verified = self.client.post(
            "/api/v1/auth/otp/verify/",
            data={"email": email, "code": code},
            content_type="application/json",
        )
        self.assertEqual(verified.status_code, 200)
        payload = verified.json()
        self.assertTrue(payload["access"])
        self.assertTrue(payload["refresh"])
        self.assertEqual(payload["user"]["role"], "hostel_student")
        self.assertEqual(payload["redirect"], "/student/")

        me = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {payload['access']}"
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "api-student")

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_invalid_otp_code_is_rejected(self):
        email = self.student.email
        self.client.post(
            "/api/v1/auth/otp/request/",
            data={"email": email},
            content_type="application/json",
        )
        real = latest_otp_code()
        wrong = "000000" if real != "000000" else "111111"
        response = self.client.post(
            "/api/v1/auth/otp/verify/",
            data={"email": email, "code": wrong},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "invalid")


    def test_student_can_upsert_intent_and_check_in(self):
        self.client.force_login(self.student)
        intent = self.client.post(
            "/api/v1/intents/",
            data={
                "date": BASE_DATE.isoformat(),
                "meal": "lunch",
                "status": "NO",
                "source": "one_tap",
            },
            content_type="application/json",
        )
        self.assertEqual(intent.status_code, 201)
        self.assertEqual(intent.json()["status"], "NO")

        checkin = self.client.post(
            "/api/v1/check-in/",
            data={
                "date": BASE_DATE.isoformat(),
                "meal": "lunch",
                "dish_ids": [self.dish.pk],
                "clean_plate": True,
            },
            content_type="application/json",
        )
        self.assertEqual(checkin.status_code, 201)
        self.assertEqual(checkin.json()["checkin"]["meal"], "lunch")
        self.assertEqual(checkin.json()["checkin"]["dishes_taken"][0]["name"], self.dish.name)

    def test_manager_can_approve_and_override_a_plan(self):
        self.client.force_login(self.manager)
        approve = self.client.post(
            f"/api/v1/manager/preparation-plan/{self.plan.pk}/approve/",
            data={"reason": "Counts look right"},
            content_type="application/json",
        )
        self.assertEqual(approve.status_code, 200)
        self.assertEqual(approve.json()["status"], "approved")

        override = self.client.post(
            f"/api/v1/manager/preparation-plan/{self.plan.pk}/override/",
            data={"recommended_kg": "18.00", "reason": "Sports fest"},
            content_type="application/json",
        )
        self.assertEqual(override.status_code, 200)
        self.assertEqual(override.json()["status"], "overridden")
        self.assertEqual(override.json()["recommended_kg"], 18.0)

    def test_manager_csv_import_dry_run(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.manager)
        content = (
            "date,meal,dish,prepared_kg,consumed_kg,wasted_kg\n"
            f"{BASE_DATE.isoformat()},lunch,{self.dish.name},10,7,3\n"
        )
        upload = SimpleUploadedFile("waste.csv", content.encode(), content_type="text/csv")
        response = self.client.post(
            "/api/v1/manager/import-csv/",
            data={"kind": "wastage", "dry_run": "true", "file": upload},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["created"], 1)
        self.assertTrue(response.json()["dry_run"])

    def test_schema_and_docs_render(self):
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)
        self.assertEqual(self.client.get("/api/docs/").status_code, 200)


class WebRoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Rajma")
        cls.student = make_student("web-student")
        cls.manager = make_manager("web-manager")
        set_today(BASE_DATE)
        make_menu(BASE_DATE, cls.slots["lunch"], [(cls.dish, 20)])
        make_forecast(BASE_DATE, cls.slots["lunch"], participation=100, demand=20)
        make_forecast(
            BASE_DATE, cls.slots["lunch"], dish=cls.dish, participation=100, demand=20
        )
        make_plan(BASE_DATE, cls.slots["lunch"], cls.dish, 16, 20)

    def test_student_home_renders(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:student-home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lunch")

    def test_student_cannot_open_manager_page(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:manager-dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_open_student_home(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("web:student-home"))
        self.assertEqual(response.status_code, 403)

    def test_manager_dashboard_renders(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("web:manager-dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preparation")

    def test_login_page_shows_email_sign_in(self):
        response = self.client.get(reverse("web:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send me a code")
        self.assertNotContains(response, "student@messlab.local")
        self.assertNotContains(response, "manager@messlab.local")

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_otp_login_lands_on_the_role_page(self):
        email = self.student.email
        requested = self.client.post(
            reverse("web:login"), {"step": "request", "email": email}
        )
        self.assertEqual(requested.status_code, 200)
        self.assertNotIn("dev_hint", requested.context)
        code = latest_otp_code()
        self.assertRegex(code, r"^\d{6}$")

        response = self.client.post(
            reverse("web:login"),
            {"step": "verify", "email": email, "code": code},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], "/student/")

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_manager_otp_login_lands_on_the_manager_page(self):
        email = self.manager.email
        self.client.post(
            reverse("web:login"), {"step": "request", "email": email}
        )
        code = latest_otp_code()
        response = self.client.post(
            reverse("web:login"),
            {"step": "verify", "email": email, "code": code},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], "/manager/")

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_legacy_password_login_still_lands_on_the_role_page(self):
        response = self.client.post(
            reverse("web:login-password"),
            {"username": "web-student", "password": "test-pass-123"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], "/student/")

    @override_settings(MESSLAB=AUTH_SETTINGS)
    def test_legacy_manager_login_lands_on_the_manager_page(self):
        response = self.client.post(
            reverse("web:login-password"),
            {"username": "web-manager", "password": "test-pass-123"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], "/manager/")

    def test_password_login_can_be_disabled(self):
        with override_settings(
            MESSLAB={**settings.MESSLAB, "ALLOW_PASSWORD_LOGIN": False}
        ):
            response = self.client.get(reverse("web:login-password"))
        self.assertRedirects(response, reverse("web:login"))

    def test_manifest_and_service_worker(self):
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("manifest", manifest["Content-Type"])
        self.assertIn("name", manifest.json())

        worker = self.client.get("/sw.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn("javascript", worker["Content-Type"])
        self.assertIn("messlab-v1", worker.content.decode())
        self.assertIn("/static/", worker.content.decode())

        offline = self.client.get("/offline/")
        self.assertEqual(offline.status_code, 200)
