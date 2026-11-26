"""Self-registration for student roles, verified by email OTP.

A registration creates an inactive user immediately (so the existing OTP
machinery can issue and verify a code) and only activates it after the code is
confirmed. Admin-deactivated accounts are never reactivated this way.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import AccountAudit, LoginEvent, OTPCode, Profile, StudentProfile, User
from .otp import OTPChannel, _client_ip, issue_otp, mask_email, verify_otp

# Only these roles may be chosen on the public form. Staff roles are assigned by
# an administrator from the user-management page.
SELF_REGISTER_ROLES = (
    User.Role.HOSTEL_STUDENT,
    User.Role.STUDENT,
    User.Role.FACULTY,
)

_CODE_PREFIX = {
    User.Role.HOSTEL_STUDENT: "H",
    User.Role.STUDENT: "S",
    User.Role.FACULTY: "F",
}

CONSENT_VERSION = "2026-11"


@dataclass
class RegistrationResult:
    status: str
    user: User | None = None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0][:150], ""
    return parts[0][:150], parts[1][:150]


def unique_username(email: str) -> str:
    base = slugify(normalize_email(email).split("@", 1)[0])[:120] or "member"
    candidate = base
    suffix = 1
    while User.objects.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f"{base}{suffix}"[:150]
    return candidate


def next_student_code(role: str) -> str:
    prefix = _CODE_PREFIX.get(role, "S")
    last = (
        StudentProfile.objects.filter(student_code__startswith=f"{prefix}-")
        .order_by("-student_code")
        .values_list("student_code", flat=True)
        .first()
    )
    number = 1
    if last and "-" in last:
        try:
            number = int(last.split("-", 1)[1]) + 1
        except ValueError:
            number = 1
    return f"{prefix}-{number:05d}"


def _apply_profile(
    user: User,
    *,
    full_name: str,
    role: str,
    hostel: str | None,
    diet: str,
    phone: str,
    consent: bool = True,
) -> None:
    first_name, last_name = split_name(full_name)
    user.first_name = first_name
    user.last_name = last_name
    user.role = role
    user.save(update_fields=["first_name", "last_name", "role"])

    profile, _ = Profile.objects.get_or_create(user=user)
    profile.role = role
    profile.phone = phone
    profile.hostel = hostel if role == User.Role.HOSTEL_STUDENT else None
    profile.is_pending = True
    if consent:
        profile.data_consent_at = timezone.now()
        profile.data_consent_version = CONSENT_VERSION
    profile.save()

    if role in SELF_REGISTER_ROLES:
        student, created = StudentProfile.objects.get_or_create(
            user=user,
            defaults={
                "student_code": next_student_code(role),
                "hostel": hostel if role == User.Role.HOSTEL_STUDENT else None,
                "diet": diet,
            },
        )
        if not created:
            student.hostel = hostel if role == User.Role.HOSTEL_STUDENT else None
            student.diet = diet
            student.save(update_fields=["hostel", "diet"])


def start_registration(
    *,
    email: str,
    full_name: str,
    role: str,
    hostel: str | None = None,
    diet: str = StudentProfile.Diet.VEG,
    phone: str = "",
    consent: bool = True,
    request=None,
    channel: OTPChannel | None = None,
) -> RegistrationResult:
    """Create or refresh a pending account and email a registration code.

    The response is intentionally identical whether or not the email is already
    registered, so the form cannot be used to enumerate accounts.
    """
    email = normalize_email(email)
    ip = _client_ip(request)

    existing = User.objects.filter(email__iexact=email).first()
    if existing:
        if existing.is_active or not getattr(existing.role_profile, "is_pending", False):
            LoginEvent.objects.create(
                email_masked=mask_email(email),
                event=LoginEvent.Event.OTP_REQUEST,
                success=False,
                ip_address=ip,
            )
            return RegistrationResult(status="sent")
        user = existing
    else:
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=unique_username(email),
                    email=email,
                    password=None,
                    role=role,
                    is_active=False,
                )
        except IntegrityError:
            # A concurrent request won the race; treat it as a duplicate.
            return RegistrationResult(status="sent")

    with transaction.atomic():
        _apply_profile(
            user,
            full_name=full_name,
            role=role,
            hostel=hostel,
            diet=diet,
            phone=phone,
            consent=consent,
        )
        result = issue_otp(
            user,
            purpose=OTPCode.Purpose.REGISTER,
            request=request,
            channel=channel,
        )
    return RegistrationResult(status=result.status, user=user)


def complete_registration(
    email: str,
    code: str,
    *,
    request=None,
) -> RegistrationResult:
    """Verify the registration code and activate the pending account."""
    email = normalize_email(email)
    user = User.objects.filter(email__iexact=email, is_active=False).first()
    if not user or not getattr(user.role_profile, "is_pending", False):
        verify_otp(None, code, purpose=OTPCode.Purpose.REGISTER, request=request)
        return RegistrationResult(status="invalid")

    result = verify_otp(
        user, code, purpose=OTPCode.Purpose.REGISTER, request=request
    )
    if result.status != "ok":
        return RegistrationResult(status=result.status)

    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=["is_active"])
        profile = user.role_profile
        profile.is_pending = False
        profile.save(update_fields=["is_pending"])
        AccountAudit.objects.create(
            actor=user,
            target=user,
            action=AccountAudit.Action.REGISTER,
            detail={"role": user.effective_role, "at": timezone.now().isoformat()},
            ip_address=_client_ip(request),
        )
    return RegistrationResult(status="ok", user=user)
