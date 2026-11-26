"""Phase 8 operations: waste, day close, menu feedback, reference data, privacy."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AccountAudit, LoginEvent, User
from dining.models import CheckIn, MealFeedback, MealService, WasteLog
from dining.services import (
    close_service_day,
    feedback_counts,
    record_waste,
    save_meal_feedback,
    service_day_summary,
    today,
)
from insights.privacy import delete_person, export_person, purge_older_than
from tests.factories import (
    BASE_DATE,
    make_checkin,
    make_dish,
    make_intent,
    make_manager,
    make_menu,
    make_slots,
    make_student,
    make_user,
    set_today,
)


class OperationsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        set_today(BASE_DATE)
        cls.student = make_student("ops-student", hostel="H1")
        cls.other_student = make_student("ops-student-2", hostel="H2")
        cls.manager = make_manager("ops-manager")
        cls.canteen = make_user("ops-canteen", User.Role.CANTEEN_STAFF)
        cls.dish = make_dish("Rajma", portion=180)

    @classmethod
    def tearDownClass(cls):
        set_today(None)
        super().tearDownClass()


class WasteEntryTests(OperationsBase):
    def test_record_waste_creates_one_row_and_updates_it_in_place(self):
        first = record_waste(
            BASE_DATE,
            self.slots["lunch"],
            self.dish,
            Decimal("20"),
            Decimal("17.5"),
            Decimal("2.5"),
            actor=self.manager,
        )
        second = record_waste(
            BASE_DATE,
            self.slots["lunch"],
            self.dish,
            Decimal("22"),
            Decimal("19"),
            Decimal("3"),
            actor=self.manager,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WasteLog.objects.count(), 1)
        self.assertEqual(second.wasted_kg, Decimal("3.00"))
        self.assertTrue(
            AccountAudit.objects.filter(
                action=AccountAudit.Action.WASTE_SAVE
            ).exists()
        )

    def test_impossible_amounts_are_rejected(self):
        with self.assertRaisesMessage(ValueError, "cannot exceed prepared"):
            record_waste(
                BASE_DATE,
                self.slots["lunch"],
                self.dish,
                Decimal("10"),
                Decimal("12"),
                Decimal("1"),
            )
        self.assertFalse(WasteLog.objects.exists())

    def test_a_future_day_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "today or an earlier day"):
            record_waste(
                BASE_DATE + dt.timedelta(days=1),
                self.slots["lunch"],
                self.dish,
                Decimal("10"),
                Decimal("8"),
                Decimal("2"),
            )

    def test_manager_can_record_waste_from_the_page(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-waste-entry"),
            {
                "date": BASE_DATE.isoformat(),
                "meal": self.slots["lunch"].pk,
                "dish": self.dish.pk,
                "prepared_kg": "20.00",
                "consumed_kg": "17.50",
                "wasted_kg": "2.50",
            },
            follow=True,
        )
        self.assertContains(response, "Waste recorded")
        log = WasteLog.objects.get()
        self.assertEqual(log.dish, self.dish)
        self.assertEqual(log.wasted_kg, Decimal("2.50"))

    def test_students_cannot_open_or_use_the_waste_page(self):
        self.client.force_login(self.student)
        self.assertEqual(
            self.client.get(reverse("web:manager-waste-entry")).status_code, 403
        )
        response = self.client.post(
            reverse("web:manager-waste-entry"),
            {"date": BASE_DATE.isoformat()},
        )
        self.assertEqual(response.status_code, 403)

    def test_api_record_waste(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            "/api/v1/manager/waste/record/",
            {
                "date": BASE_DATE.isoformat(),
                "meal": "lunch",
                "dish": self.dish.pk,
                "prepared_kg": "20.00",
                "consumed_kg": "17.00",
                "wasted_kg": "3.00",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["wasted_kg"], "3.00")
        self.assertTrue(WasteLog.objects.exists())

    def test_api_record_waste_requires_the_permission(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/api/v1/manager/waste/record/",
            {
                "date": BASE_DATE.isoformat(),
                "meal": "lunch",
                "dish": self.dish.pk,
                "prepared_kg": "20.00",
                "consumed_kg": "17.00",
                "wasted_kg": "3.00",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class ServiceCloseTests(OperationsBase):
    def test_closing_a_day_writes_meal_services_and_is_idempotent(self):
        make_menu(BASE_DATE, self.slots["lunch"], [(self.dish, 20)])
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        make_intent(self.student, BASE_DATE, self.slots["lunch"])

        summary = close_service_day(BASE_DATE, actor=self.manager)
        self.assertEqual(summary["meals"], 1)
        self.assertEqual(summary["checkins"], 1)
        self.assertEqual(summary["positive_intents"], 1)
        service = MealService.objects.get(date=BASE_DATE, meal=self.slots["lunch"])
        self.assertEqual(service.actual_participants, 1)
        self.assertEqual(service.declared_intents, 1)

        close_service_day(BASE_DATE, actor=self.manager)
        self.assertEqual(MealService.objects.count(), 1)
        self.assertTrue(
            AccountAudit.objects.filter(
                action=AccountAudit.Action.SERVICE_CLOSE
            ).exists()
        )

    def test_a_future_day_cannot_be_closed(self):
        with self.assertRaisesMessage(ValueError, "future service day"):
            close_service_day(BASE_DATE + dt.timedelta(days=1))

    def test_summary_lists_every_slot(self):
        rows = service_day_summary(BASE_DATE)
        self.assertEqual(len(rows), len(self.slots))
        self.assertEqual(
            {row["slot"].name for row in rows}, set(self.slots)
        )

    def test_manager_closes_the_day_from_the_page(self):
        make_menu(BASE_DATE, self.slots["lunch"], [(self.dish, 20)])
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-close-day"),
            {"date": BASE_DATE.isoformat()},
            follow=True,
        )
        self.assertContains(response, "Closed")
        self.assertTrue(MealService.objects.filter(date=BASE_DATE).exists())

    def test_supervisors_cannot_close_the_day(self):
        supervisor = make_user("ops-supervisor", User.Role.SUPERVISOR)
        self.client.force_login(supervisor)
        self.assertEqual(
            self.client.get(reverse("web:manager-close-day")).status_code, 403
        )

    def test_api_close_day(self):
        make_menu(BASE_DATE, self.slots["dinner"], [(self.dish, 20)])
        make_checkin(self.student, BASE_DATE, self.slots["dinner"])
        self.client.force_login(self.manager)
        response = self.client.post(
            "/api/v1/manager/close-day/",
            {"date": BASE_DATE.isoformat()},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["checkins"], 1)


class MealFeedbackTests(OperationsBase):
    def test_feedback_upserts_one_answer_per_day_and_meal(self):
        save_meal_feedback(self.student, BASE_DATE, self.slots["lunch"], "yes", "keep it")
        save_meal_feedback(self.student, BASE_DATE, self.slots["lunch"], "no", "pulao")
        row = MealFeedback.objects.get()
        self.assertEqual(row.response, MealFeedback.Response.NO)
        self.assertEqual(row.suggestion, "pulao")
        self.assertEqual(
            feedback_counts(BASE_DATE, self.slots["lunch"]),
            {"yes": 0, "no": 1, "TOTAL": 1},
        )

    def test_an_invalid_answer_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "Choose yes or no"):
            save_meal_feedback(
                self.student, BASE_DATE, self.slots["lunch"], "maybe", ""
            )

    def test_student_submits_feedback_from_the_page(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("web:student-feedback"),
            {
                "date": BASE_DATE.isoformat(),
                "meal": self.slots["lunch"].name,
                "response": "no",
                "suggestion": "Make chole instead",
            },
            follow=True,
        )
        self.assertContains(response, "menu feedback")
        row = MealFeedback.objects.get(student=self.student)
        self.assertEqual(row.suggestion, "Make chole instead")

    def test_feedback_api(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/api/v1/meal-feedback/",
            {
                "date": BASE_DATE.isoformat(),
                "meal": "lunch",
                "response": "yes",
                "suggestion": "More roti",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["suggestion"], "More roti")

    def test_manager_sees_feedback_on_the_daily_menu_page(self):
        make_menu(BASE_DATE, self.slots["lunch"], [(self.dish, 20)])
        save_meal_feedback(self.student, BASE_DATE, self.slots["lunch"], "no", "pulao")
        self.client.force_login(self.manager)
        response = self.client.get(
            f"{reverse('web:manager-menu-day')}?date={BASE_DATE.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rajma")
        self.assertContains(response, "pulao")
        self.assertContains(response, "1")

    def test_students_cannot_open_the_manager_menu_page(self):
        self.client.force_login(self.student)
        self.assertEqual(
            self.client.get(reverse("web:manager-menu-day")).status_code, 403
        )


class ReferenceDataTests(OperationsBase):
    def test_manager_adds_a_dish(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-data"),
            {
                "action": "dish-save",
                "name": "Chole Bhature",
                "category": "main",
                "diet_type": "veg",
                "avg_portion_grams": "220",
                "popularity_baseline": "0.5",
                "is_active": "on",
            },
            follow=True,
        )
        self.assertContains(response, "saved")
        self.assertTrue(
            AccountAudit.objects.filter(action=AccountAudit.Action.DISH_SAVE).exists()
        )
        from menu.models import Dish

        self.assertTrue(Dish.objects.filter(name="Chole Bhature").exists())

    def test_manager_adds_a_meal_slot(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-data"),
            {
                "action": "slot-save",
                "name": "midnight",
                "start_time": "23:30",
                "end_time": "23:59",
                "peak_start": "23:35",
                "peak_end": "23:50",
                "counters": "1",
                "counter_capacity_per_hour": "120",
                "order": "9",
            },
            follow=True,
        )
        self.assertContains(response, "saved")
        from menu.models import MealSlot

        self.assertTrue(MealSlot.objects.filter(name="midnight").exists())
        self.assertTrue(
            AccountAudit.objects.filter(action=AccountAudit.Action.SLOT_SAVE).exists()
        )

    def test_students_cannot_edit_reference_data(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("web:manager-data")).status_code, 403)


class PrivacyTests(OperationsBase):
    def test_export_contains_only_the_signed_in_account(self):
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        save_meal_feedback(self.student, BASE_DATE, self.slots["lunch"], "yes")
        payload = export_person(self.student)
        self.assertEqual(payload["account"]["email"], self.student.email)
        self.assertEqual(len(payload["check_ins"]), 1)
        self.assertEqual(len(payload["menu_feedback"]), 1)

    def test_export_download(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:privacy-export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        payload = json.loads(response.content)
        self.assertEqual(payload["account"]["username"], self.student.username)

    def test_delete_requires_the_typed_confirmation(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("web:privacy-delete"), {"confirm": "nope"}, follow=True
        )
        self.assertContains(response, "Type DELETE")
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())

    def test_delete_removes_the_account_and_its_records(self):
        make_checkin(self.student, BASE_DATE, self.slots["lunch"])
        save_meal_feedback(self.student, BASE_DATE, self.slots["lunch"], "yes")
        pk = self.student.pk
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("web:privacy-delete"), {"confirm": "DELETE"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=pk).exists())
        self.assertFalse(CheckIn.objects.exists())
        self.assertFalse(MealFeedback.objects.exists())

    def test_api_delete_requires_confirmation(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/api/v1/privacy/delete/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())

    def test_purge_reports_before_deleting(self):
        event = LoginEvent.objects.create(event=LoginEvent.Event.OTP_REQUEST)
        LoginEvent.objects.filter(pk=event.pk).update(
            at=timezone.now() - dt.timedelta(days=400)
        )
        result = purge_older_than(180, dry_run=True)
        self.assertGreaterEqual(result["login_events"], 1)
        self.assertTrue(LoginEvent.objects.filter(pk=event.pk).exists())
        purge_older_than(180)
        self.assertFalse(LoginEvent.objects.filter(pk=event.pk).exists())


class HealthAndHeadersTests(TestCase):
    def test_healthz_does_not_need_a_login(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readyz_reports_the_database(self):
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["database"], "ok")

    def test_app_pages_get_a_restrictive_csp(self):
        response = self.client.get("/login/")
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("cdn.jsdelivr.net", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src", 1)[1].split(";", 1)[0])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(
            response["Permissions-Policy"], "camera=(self), microphone=(), geolocation=()"
        )

    def test_admin_pages_allow_inline_scripts(self):
        self.client.force_login(make_manager("header-manager"))
        response = self.client.get("/admin/")
        self.assertIn("'unsafe-inline'", response["Content-Security-Policy"])

    def test_api_docs_use_vendored_swagger_assets(self):
        response = self.client.get("/api/docs/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "/static/vendor/swagger-ui/swagger-ui-bundle.js"
        )
        self.assertNotContains(response, "cdn.jsdelivr.net")
        self.assertNotContains(response, "validator.swagger.io")
        self.assertNotIn("cdn.jsdelivr.net", response["Content-Security-Policy"])


class ScannerVendorTests(OperationsBase):
    def test_scanner_page_ships_a_local_qr_decoder(self):
        self.client.force_login(self.canteen)
        response = self.client.get(reverse("web:staff-scan"))
        self.assertContains(response, "vendor/jsQR.js")
        self.assertContains(response, "js/scan.js")


class RegistrationSwitchTests(TestCase):
    def test_self_registration_can_be_closed(self):
        closed = {**settings.MESSLAB, "ALLOW_SELF_REGISTRATION": False}
        with override_settings(MESSLAB=closed):
            response = self.client.get("/register/")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], reverse("web:login"))
            api = self.client.post(
                "/api/v1/auth/register/",
                {
                    "full_name": "Asha Verma",
                    "email": "asha@college.test",
                    "role": "hostel_student",
                    "hostel": "H2",
                },
                content_type="application/json",
            )
            self.assertEqual(api.status_code, 403)
