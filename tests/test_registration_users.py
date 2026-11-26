"""Phase 2: self-registration, account administration and audit."""
from __future__ import annotations

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from accounts.authentication import issue_tokens
from accounts.models import AccountAudit, OTPCode, Profile, User
from tests.factories import latest_otp_code, make_student, make_user

REGISTRATION = {
    "full_name": "Asha Verma",
    "email": "asha@college.test",
    "role": "hostel_student",
    "hostel": "H2",
    "diet": "veg",
    "phone": "+919000000000",
    "consent": "on",
}


class RegistrationTests(TestCase):
    def setUp(self):
        cache.clear()

    def _start(self, **overrides):
        payload = {**REGISTRATION, **overrides}
        return self.client.post(
            "/api/v1/auth/register/", payload, content_type="application/json"
        )

    def _verify(self, email: str, code: str):
        return Client().post(
            "/api/v1/auth/register/verify/",
            {"email": email, "code": code},
            content_type="application/json",
        )

    def test_registration_creates_pending_then_active_user(self):
        response = self._start()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dev_hint", response.json())
        code = latest_otp_code()
        self.assertRegex(code, r"^\d{6}$")

        user = User.objects.get(email="asha@college.test")
        self.assertFalse(user.is_active)
        self.assertTrue(user.role_profile.is_pending)
        self.assertEqual(user.effective_role, User.Role.HOSTEL_STUDENT)
        self.assertEqual(user.profile.hostel, "H2")
        self.assertRegex(user.profile.student_code, r"^H-\d{5}$")
        self.assertFalse(user.has_usable_password())

        verified = self._verify("asha@college.test", code)
        self.assertEqual(verified.status_code, 201)
        self.assertTrue(verified.json()["access"])
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(user.role_profile.is_pending)
        self.assertTrue(
            AccountAudit.objects.filter(
                target=user, action=AccountAudit.Action.REGISTER
            ).exists()
        )

    def test_hostel_is_required_for_hostel_students(self):
        response = self._start(hostel="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("hostel", response.json())
        self.assertFalse(User.objects.filter(email="asha@college.test").exists())

    def test_staff_roles_cannot_self_register(self):
        response = self._start(role=User.Role.MANAGER)
        self.assertEqual(response.status_code, 400)

    def test_existing_active_email_is_generic_and_untouched(self):
        existing = make_student("existing", email="asha@college.test")
        before = OTPCode.objects.count()
        response = self._start()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dev_hint", response.json())
        self.assertEqual(User.objects.filter(email="asha@college.test").count(), 1)
        self.assertEqual(OTPCode.objects.count(), before)
        existing.refresh_from_db()
        self.assertTrue(existing.is_active)

    def test_admin_deactivated_account_cannot_self_reactivate(self):
        user = make_student("disabled", email="asha@college.test")
        user.is_active = False
        user.save(update_fields=["is_active"])
        Profile.objects.filter(user=user).update(is_pending=False)

        response = self._start()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dev_hint", response.json())
        self.assertEqual(OTPCode.objects.filter(user=user).count(), 0)

        verified = self._verify("asha@college.test", "123456")
        self.assertEqual(verified.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_wrong_registration_code_does_not_activate(self):
        self._start()
        code = latest_otp_code()
        wrong = "000000" if code != "000000" else "111111"
        response = self._verify("asha@college.test", wrong)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.get(email="asha@college.test").is_active)

    @override_settings(OTP_RESEND_COOLDOWN=0)
    def test_pending_registration_can_resend(self):
        self._start()
        first = latest_otp_code()
        self._start()
        second = latest_otp_code(index=-1)
        self.assertNotEqual(first, second)
        self.assertEqual(User.objects.filter(email="asha@college.test").count(), 1)

    def test_web_registration_flow(self):
        page = self.client.get("/register/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Create your account")

        requested = self.client.post(
            "/register/", {"step": "request", **REGISTRATION}
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.context["step"], "verify")
        self.assertNotIn("dev_hint", requested.context)
        code = latest_otp_code()

        verified = self.client.post(
            "/register/",
            {"step": "verify", "email": "asha@college.test", "code": code},
            follow=True,
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.redirect_chain[-1][0], "/student/")
        self.assertTrue(User.objects.get(email="asha@college.test").is_active)

    def test_registration_response_never_returns_the_code(self):
        response = self._start()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dev_hint", response.json())
        self.assertNotIn(latest_otp_code(), response.content.decode())


class AccountAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("admin-user", User.Role.ADMIN, email="admin@messlab.test")
        cls.manager = make_user("mgr", User.Role.MANAGER, email="mgr@messlab.test")
        cls.supervisor = make_user(
            "sup", User.Role.SUPERVISOR, email="sup@messlab.test"
        )
        cls.student = make_student("stu", email="stu@messlab.test", hostel="H1")
        cls.student.first_name = "Private"
        cls.student.last_name = "Name"
        cls.student.save(update_fields=["first_name", "last_name"])
        cls.worker = make_user("worker", User.Role.WORKER, email="worker@messlab.test")

    def test_manager_list_is_pseudonymous(self):
        self.client.force_login(self.manager)
        response = self.client.get("/api/v1/manager/users/")
        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.json()["results"]
            if item["student_code"] == self.student.profile.student_code
        )
        self.assertEqual(row["full_name"], "")
        self.assertEqual(row["username"], "")
        self.assertNotIn("Private", str(response.json()))
        self.assertNotIn("stu@messlab.test", str(response.json()))
        self.assertIn("s***@messlab.test", row["email"])

        page = self.client.get("/manager/users/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.student.profile.student_code)
        self.assertNotContains(page, "Private")

    def test_admin_list_shows_identity(self):
        self.client.force_login(self.admin)
        response = self.client.get("/api/v1/manager/users/")
        row = next(
            item
            for item in response.json()["results"]
            if item["student_code"] == self.student.profile.student_code
        )
        self.assertEqual(row["full_name"], "Private Name")
        self.assertEqual(row["email"], "stu@messlab.test")

    def test_supervisor_only_sees_staff_roster(self):
        self.client.force_login(self.supervisor)
        response = self.client.get("/api/v1/manager/users/")
        self.assertEqual(response.status_code, 200)
        codes = {row["student_code"] for row in response.json()["results"]}
        self.assertIn(f"U-{self.worker.pk:05d}", codes)
        self.assertNotIn(self.student.profile.student_code, codes)

    def test_manager_cannot_change_roles(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            f"/api/v1/manager/users/{self.student.pk}/role/",
            {"role": User.Role.FACULTY},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_changes_role_and_invalidates_tokens(self):
        tokens = issue_tokens(self.student)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/v1/manager/users/{self.student.pk}/role/",
            {"role": User.Role.FACULTY},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.effective_role, User.Role.FACULTY)

        stale = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )
        self.assertEqual(stale.status_code, 401)
        entry = AccountAudit.objects.get(
            target=self.student, action=AccountAudit.Action.ROLE_CHANGE
        )
        self.assertEqual(entry.detail, {"from": "hostel_student", "to": "faculty"})

    def test_admin_cannot_change_own_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/v1/manager/users/{self.admin.pk}/role/",
            {"role": User.Role.STUDENT},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_non_superuser_cannot_grant_admin(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/v1/manager/users/{self.student.pk}/role/",
            {"role": User.Role.ADMIN},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_can_deactivate_and_login_is_blocked(self):
        tokens = issue_tokens(self.student)
        self.client.force_login(self.manager)
        response = self.client.post(
            f"/api/v1/manager/users/{self.student.pk}/active/",
            {"active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

        stale = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )
        self.assertEqual(stale.status_code, 401)
        self.assertTrue(
            AccountAudit.objects.filter(
                target=self.student, action=AccountAudit.Action.DEACTIVATE
            ).exists()
        )

    def test_manager_cannot_disable_an_admin(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            f"/api/v1/manager/users/{self.admin.pk}/active/",
            {"active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_student_cannot_reach_account_pages(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get("/manager/users/").status_code, 403)
        self.assertEqual(self.client.get("/manager/audit/").status_code, 403)
        self.assertEqual(
            self.client.get("/api/v1/manager/users/").status_code, 403
        )

    def test_audit_page_is_visible_to_manager(self):
        AccountAudit.objects.create(
            actor=self.manager,
            target=self.student,
            action=AccountAudit.Action.DEACTIVATE,
            detail={"active": False},
        )
        self.client.force_login(self.manager)
        response = self.client.get("/manager/audit/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.student.profile.student_code)
        self.assertContains(response, "Deactivated")
        self.assertNotContains(response, "Private")

    def test_web_role_change_requires_admin(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            f"/manager/users/{self.student.pk}/role/", {"role": User.Role.FACULTY}
        )
        self.assertEqual(response.status_code, 403)

    def test_web_admin_can_change_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/manager/users/{self.student.pk}/role/", {"role": User.Role.STUDENT}
        )
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.effective_role, User.Role.STUDENT)
