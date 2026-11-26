"""Phase 1: email OTP issuance/verification and JWT session behaviour."""
from __future__ import annotations

import datetime as dt
import io
from contextlib import redirect_stdout

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.authentication import issue_tokens
from accounts.models import LoginEvent, OTPCode
from accounts.otp import (
    EmailOTPChannel,
    OTPChannel,
    issue_otp,
    request_login_otp,
    verify_otp,
)
from tests.factories import make_manager, make_student


class CaptureChannel(OTPChannel):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str]] = []

    def send(self, user, code: str, purpose: str) -> None:
        self.sent.append((user.pk, code, purpose))


class OTPTests(TestCase):
    def setUp(self):
        cache.clear()
        self.student = make_student("otp-student", email="otp.student@messlab.test")
        self.channel = CaptureChannel()

    def _issue(self, **kwargs) -> str:
        result = issue_otp(
            self.student,
            channel=self.channel,
            enforce_cooldown=kwargs.pop("enforce_cooldown", False),
            **kwargs,
        )
        self.assertEqual(result.status, "sent")
        return self.channel.sent[-1][1]

    def test_only_a_digest_is_stored(self):
        code = self._issue()
        row = OTPCode.objects.get(user=self.student)
        self.assertEqual(len(row.code_hash), 64)
        self.assertNotEqual(row.code_hash, code)
        self.assertTrue(row.salt)
        self.assertEqual(row.purpose, OTPCode.Purpose.LOGIN)

    def test_verify_accepts_a_code_once(self):
        code = self._issue()
        first = verify_otp(self.student, code)
        self.assertEqual(first.status, "ok")
        second = verify_otp(self.student, code)
        self.assertEqual(second.status, "expired")

    def test_wrong_code_is_rejected_and_counted(self):
        real = self._issue()
        wrong = "000000" if real != "000000" else "111111"
        result = verify_otp(self.student, wrong)
        self.assertEqual(result.status, "invalid")
        row = OTPCode.objects.get(user=self.student)
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.consumed_at)

    @override_settings(OTP_MAX_ATTEMPTS=3)
    def test_lockout_after_max_attempts(self):
        real = self._issue()
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(2):
            self.assertEqual(verify_otp(self.student, wrong).status, "invalid")
        self.assertEqual(verify_otp(self.student, wrong).status, "lockout")
        # Even the correct code is refused once the row is locked.
        self.assertEqual(verify_otp(self.student, real).status, "lockout")
        self.assertTrue(
            LoginEvent.objects.filter(
                user=self.student, event=LoginEvent.Event.LOCKOUT
            ).exists()
        )

    def test_expired_code_is_rejected(self):
        code = self._issue()
        OTPCode.objects.filter(user=self.student).update(
            expires_at=timezone.now() - dt.timedelta(seconds=1)
        )
        self.assertEqual(verify_otp(self.student, code).status, "expired")

    def test_resend_cooldown(self):
        self._issue()
        again = issue_otp(self.student, channel=self.channel, enforce_cooldown=True)
        self.assertEqual(again.status, "cooldown")
        self.assertEqual(OTPCode.objects.filter(user=self.student).count(), 1)

    def test_new_code_consumes_the_previous_one(self):
        first = self._issue()
        second = self._issue()
        self.assertNotEqual(first, second)
        self.assertEqual(
            OTPCode.objects.filter(user=self.student, consumed_at__isnull=True).count(), 1
        )
        # Only the newest unconsumed row is compared, so the old code is wrong.
        self.assertEqual(verify_otp(self.student, first).status, "invalid")
        self.assertEqual(verify_otp(self.student, second).status, "ok")

    def test_unknown_email_does_not_enumerate(self):
        known = request_login_otp(self.student.email, channel=self.channel)
        unknown = request_login_otp("nobody@messlab.test", channel=self.channel)
        self.assertEqual(known.status, unknown.status)
        self.assertIsNone(unknown.user)
        self.assertFalse(
            LoginEvent.objects.filter(
                email_masked="n***@messlab.test", success=True
            ).exists()
        )

    def test_request_is_case_insensitive(self):
        result = request_login_otp("OTP.STUDENT@MESSLAB.TEST", channel=self.channel)
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.user, self.student)
        self.assertEqual(OTPCode.objects.filter(user=self.student).count(), 1)

    @override_settings(OTP_RESEND_COOLDOWN=0, OTP_EMAIL_THROTTLE=2)
    def test_email_throttle_caps_issuance(self):
        for _ in range(3):
            self.assertEqual(
                request_login_otp(self.student.email, channel=self.channel).status,
                "sent",
            )
        self.assertEqual(OTPCode.objects.filter(user=self.student).count(), 2)
        self.assertTrue(
            LoginEvent.objects.filter(event=LoginEvent.Event.LOCKOUT).exists()
        )

    @override_settings(OTP_RESEND_COOLDOWN=0, OTP_IP_THROTTLE=1, OTP_EMAIL_THROTTLE=99)
    def test_ip_throttle_caps_issuance(self):
        from django.test import RequestFactory

        other = make_student("otp-other", email="other@messlab.test")
        request = RequestFactory().post("/login/")
        self.assertEqual(
            request_login_otp(
                self.student.email, request=request, channel=self.channel
            ).status,
            "sent",
        )
        self.assertEqual(
            request_login_otp(
                other.email, request=request, channel=self.channel
            ).status,
            "sent",
        )
        self.assertEqual(OTPCode.objects.filter(user=other).count(), 0)


class DevelopmentCodeOutputTests(TestCase):
    def setUp(self):
        self.student = make_student("dev-code", email="dev.code@messlab.test")

    @override_settings(
        DEBUG=True,
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    )
    def test_console_email_backend_prints_a_clear_code_line(self):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            EmailOTPChannel().send(self.student, "123456", OTPCode.Purpose.LOGIN)

        output = buffer.getvalue()
        self.assertIn("MessLab development code", output)
        self.assertIn("123456", output)
        self.assertIn(self.student.email, output)

    @override_settings(DEBUG=True)
    def test_non_console_backend_never_prints_the_code(self):
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            EmailOTPChannel().send(self.student, "654321", OTPCode.Purpose.LOGIN)

        self.assertNotIn("MessLab development code", buffer.getvalue())
        self.assertNotIn("654321", buffer.getvalue())


class JWTTests(TestCase):
    def setUp(self):
        cache.clear()
        self.student = make_student("jwt-student", email="jwt.student@messlab.test")
        self.manager = make_manager("jwt-manager", email="jwt.manager@messlab.test")

    def test_access_token_authenticates(self):
        tokens = issue_tokens(self.student)
        response = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "jwt-student")

    def test_refresh_rotates_and_blacklists_the_old_token(self):
        tokens = issue_tokens(self.student)
        refreshed = Client().post(
            "/api/v1/auth/token/refresh/",
            data={"refresh": tokens["refresh"]},
            content_type="application/json",
        )
        self.assertEqual(refreshed.status_code, 200)
        payload = refreshed.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)
        self.assertNotEqual(payload["refresh"], tokens["refresh"])

        reused = Client().post(
            "/api/v1/auth/token/refresh/",
            data={"refresh": tokens["refresh"]},
            content_type="application/json",
        )
        self.assertEqual(reused.status_code, 401)

    def test_role_change_invalidates_outstanding_tokens(self):
        tokens = issue_tokens(self.student)
        profile = self.student.role_profile
        profile.role = "faculty"
        profile.save()
        response = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )
        self.assertEqual(response.status_code, 401)

    def test_forged_role_claim_is_ignored(self):
        token = AccessToken.for_user(self.student)
        token["token_version"] = self.student.token_version
        token["role"] = "admin"
        response = Client().get(
            "/api/v1/manager/preparation-plan/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 403)

    def test_inactive_user_token_is_rejected(self):
        tokens = issue_tokens(self.student)
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])
        response = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )
        self.assertEqual(response.status_code, 401)

    def test_logout_blacklists_the_refresh_token(self):
        tokens = issue_tokens(self.student)
        client = Client()
        client.force_login(self.student)
        out = client.post(
            "/api/v1/auth/logout/",
            data={"refresh": tokens["refresh"]},
            content_type="application/json",
        )
        self.assertEqual(out.status_code, 204)
        reused = Client().post(
            "/api/v1/auth/token/refresh/",
            data={"refresh": tokens["refresh"]},
            content_type="application/json",
        )
        self.assertEqual(reused.status_code, 401)

    def test_refresh_token_cannot_be_used_as_access(self):
        tokens = issue_tokens(self.student)
        response = Client().get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['refresh']}"
        )
        self.assertEqual(response.status_code, 401)
