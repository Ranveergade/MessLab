"""One-time codes for email login, registration and account deletion.

Only an HMAC-SHA256 digest is stored. Codes are generated with ``secrets``,
compared in constant time, single-use, attempt-limited and throttled through the
shared Django cache.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from .models import LoginEvent, OTPCode, User

logger = logging.getLogger(__name__)


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _secret_key() -> bytes:
    return hashlib.sha256(f"messlab-otp:{settings.SECRET_KEY}".encode()).digest()


def _code_hash(user_id: int, code: str, salt: str) -> str:
    message = f"{user_id}|{code}|{salt}".encode()
    return hmac.new(_secret_key(), message, hashlib.sha256).hexdigest()


def _client_ip(request) -> str | None:
    if request is None:
        return None
    if settings.MESSLAB.get("TRUST_X_FORWARDED_FOR"):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR") or None


def _throttled(scope: str, value: str, limit: int, window: int) -> bool:
    if not value:
        return False
    key = f"messlab:otp:{scope}:{value}"
    if cache.add(key, 1, window):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, window)
        return False
    return count > limit


def _log(event: str, *, user=None, email: str = "", success=False, request=None) -> None:
    LoginEvent.objects.create(
        user=user,
        email_masked=mask_email(email),
        event=event,
        success=success,
        ip_address=_client_ip(request),
        role=getattr(user, "effective_role", "") if user else "",
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:255] if request else "",
    )


class OTPChannel:
    """A delivery channel. Email is the only implementation today."""

    def send(self, user: User, code: str, purpose: str) -> None:  # pragma: no cover
        raise NotImplementedError


class EmailOTPChannel(OTPChannel):
    def send(self, user: User, code: str, purpose: str) -> None:
        ttl_minutes = max(1, settings.OTP_TTL_SECONDS // 60)
        if settings.DEBUG and settings.EMAIL_BACKEND.endswith(
            "console.EmailBackend"
        ):
            # Development convenience: make the code impossible to miss in the
            # runserver terminal (the console backend also prints the email body).
            print(
                "\n"
                "============================================================\n"
                f" MessLab development code ({purpose}): {code}\n"
                f" Account: {user.email}   Valid for {ttl_minutes} minutes\n"
                "============================================================\n",
                flush=True,
            )
        subject = "Your MessLab login code"
        text = f"Your MessLab code is {code}. It expires in {ttl_minutes} minutes."
        html = (
            f"<p>Your MessLab code is <strong style=\"font-size:24px\">{code}</strong>.</p>"
            f"<p>It expires in {ttl_minutes} minutes.</p>"
        )
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)


DEFAULT_CHANNEL: OTPChannel = EmailOTPChannel()


@dataclass
class OTPResult:
    status: str
    user: User | None = None


def issue_otp(
    user: User,
    *,
    purpose: str = OTPCode.Purpose.LOGIN,
    request=None,
    channel: OTPChannel | None = None,
    enforce_cooldown: bool = True,
) -> OTPResult:
    """Create and send a code. Returns a generic result; never exposes the code."""
    now = timezone.now()
    if enforce_cooldown:
        last = (
            OTPCode.objects.filter(user=user, purpose=purpose)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        if last and (now - last).total_seconds() < settings.OTP_RESEND_COOLDOWN:
            return OTPResult(status="cooldown")

    code = f"{secrets.randbelow(10**6):06d}"
    salt = secrets.token_hex(16)
    expires_at = now + dt.timedelta(seconds=settings.OTP_TTL_SECONDS)
    with transaction.atomic():
        OTPCode.objects.filter(
            user=user, purpose=purpose, consumed_at__isnull=True
        ).update(consumed_at=now)
        row = OTPCode.objects.create(
            user=user,
            code_hash=_code_hash(user.pk, code, salt),
            salt=salt,
            purpose=purpose,
            ip_address=_client_ip(request),
            expires_at=expires_at,
        )
    try:
        (channel or DEFAULT_CHANNEL).send(user, code, purpose)
    except Exception:  # delivery failure must not reveal whether the account exists
        logger.exception("OTP email delivery failed for user %s", user.pk)
    _log(LoginEvent.Event.OTP_REQUEST, user=user, email=user.email, success=True, request=request)
    return OTPResult(status="sent", user=user)


def request_login_otp(email: str, *, request=None, channel: OTPChannel | None = None) -> OTPResult:
    """Always returns a generic result. No user enumeration."""
    email = (email or "").strip()
    ip = _client_ip(request) or ""
    if _throttled("ip", ip, settings.OTP_IP_THROTTLE, 3600):
        _log(LoginEvent.Event.LOCKOUT, email=email, request=request)
        return OTPResult(status="sent")
    if _throttled("email", email.lower(), settings.OTP_EMAIL_THROTTLE, 3600):
        _log(LoginEvent.Event.LOCKOUT, email=email, request=request)
        return OTPResult(status="sent")

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if not user:
        # Burn comparable CPU time and return the same shape.
        _code_hash(0, "000000", secrets.token_hex(16))
        _log(LoginEvent.Event.OTP_REQUEST, email=email, success=False, request=request)
        return OTPResult(status="sent")
    issue_otp(user, purpose=OTPCode.Purpose.LOGIN, request=request, channel=channel)
    return OTPResult(status="sent", user=user)


def verify_otp(
    user: User | None,
    code: str,
    *,
    purpose: str = OTPCode.Purpose.LOGIN,
    request=None,
) -> OTPResult:
    code = (code or "").strip()
    if not user:
        _code_hash(0, code or "000000", "dummy")
        _log(LoginEvent.Event.OTP_FAILURE, request=request)
        return OTPResult(status="invalid")

    now = timezone.now()
    with transaction.atomic():
        row = (
            OTPCode.objects.select_for_update()
            .filter(user=user, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if not row or row.expires_at <= now:
            _log(LoginEvent.Event.OTP_FAILURE, user=user, email=user.email, request=request)
            return OTPResult(status="expired")
        if row.attempts >= settings.OTP_MAX_ATTEMPTS:
            _log(LoginEvent.Event.LOCKOUT, user=user, email=user.email, request=request)
            return OTPResult(status="lockout")
        expected = _code_hash(user.pk, code, row.salt)
        if not hmac.compare_digest(expected, row.code_hash):
            row.attempts += 1
            row.save(update_fields=["attempts"])
            event = (
                LoginEvent.Event.LOCKOUT
                if row.attempts >= settings.OTP_MAX_ATTEMPTS
                else LoginEvent.Event.OTP_FAILURE
            )
            _log(event, user=user, email=user.email, request=request)
            return OTPResult(status="lockout" if row.attempts >= settings.OTP_MAX_ATTEMPTS else "invalid")
        row.consumed_at = now
        row.save(update_fields=["consumed_at"])
    _log(LoginEvent.Event.OTP_SUCCESS, user=user, email=user.email, success=True, request=request)
    return OTPResult(status="ok", user=user)
