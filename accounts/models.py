"""Accounts: custom user with a role, plus the student dining profile."""
from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

WEEKDAYS = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]
MEAL_KEYS = ("breakfast", "lunch", "snacks", "dinner")
PLAN_VALUES = ("eat", "skip", "auto")


def empty_default_plan() -> dict[str, dict[str, str]]:
    return {
        str(day): {meal: "auto" for meal in MEAL_KEYS}
        for day, _label in WEEKDAYS
    }


def validate_default_plan(value) -> None:
    """Validate the nested weekly plan JSON. Raises ValidationError."""
    if value in (None, ""):
        return
    if not isinstance(value, dict):
        raise ValidationError("default_plan must be a JSON object keyed by weekday 0-6.")
    for day_key, meals in value.items():
        if str(day_key) not in {str(d) for d, _ in WEEKDAYS}:
            raise ValidationError(f"Invalid weekday key: {day_key!r}. Use 0 (Mon) to 6 (Sun).")
        if not isinstance(meals, dict):
            raise ValidationError(f"Weekday {day_key} must map meal names to eat/skip/auto.")
        for meal, plan in meals.items():
            if meal not in MEAL_KEYS:
                raise ValidationError(f"Invalid meal {meal!r}. Use one of {', '.join(MEAL_KEYS)}.")
            if plan not in PLAN_VALUES:
                raise ValidationError(
                    f"Invalid plan value {plan!r} for {meal}. Use eat, skip or auto."
                )


class User(AbstractUser):
    class Role(models.TextChoices):
        HOSTEL_STUDENT = "hostel_student", "Hostel student"
        STUDENT = "student", "Day scholar"
        FACULTY = "faculty", "Faculty"
        WORKER = "worker", "Worker (bhaiya / didi)"
        CANTEEN_STAFF = "canteen_staff", "Canteen staff"
        SUPERVISOR = "supervisor", "Supervisor"
        MANAGER = "manager", "Mess Manager"
        ADMIN = "admin", "Administrator"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STUDENT)
    token_version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["username"]
        indexes = [models.Index(fields=["role"], name="accounts_user_role_idx")]
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("email"),
                condition=~models.Q(email=""),
                name="uniq_user_email_ci",
            )
        ]

    @property
    def effective_role(self) -> str:
        """Profile is the source of truth; fall back to the legacy user column."""
        try:
            profile_role = self.role_profile.role
        except Profile.DoesNotExist:
            profile_role = ""
        return profile_role or self.role

    @property
    def is_student(self) -> bool:
        return self.effective_role in {
            self.Role.HOSTEL_STUDENT,
            self.Role.STUDENT,
            self.Role.FACULTY,
        }

    @property
    def is_manager(self) -> bool:
        return self.effective_role == self.Role.MANAGER

    @property
    def is_admin(self) -> bool:
        return self.is_superuser or self.effective_role == self.Role.ADMIN

    @property
    def is_supervisor(self) -> bool:
        return self.effective_role == self.Role.SUPERVISOR

    @property
    def is_worker(self) -> bool:
        return self.effective_role in {self.Role.WORKER, self.Role.CANTEEN_STAFF}

    @property
    def can_use_student_app(self) -> bool:
        return self.effective_role in {
            self.Role.HOSTEL_STUDENT,
            self.Role.STUDENT,
            self.Role.FACULTY,
        }

    @property
    def can_use_manager_app(self) -> bool:
        return self.is_admin or self.effective_role == self.Role.MANAGER


class StudentProfile(models.Model):
    class Hostel(models.TextChoices):
        H1 = "H1", "Hostel 1 - Ganga"
        H2 = "H2", "Hostel 2 - Yamuna"
        H3 = "H3", "Hostel 3 - Saraswati"
        H4 = "H4", "Hostel 4 - Narmada"
        H5 = "H5", "Hostel 5 - Kaveri"
        H6 = "H6", "Hostel 6 - Krishna"
        H7 = "H7", "Hostel 7 - Godavari"
        H8 = "H8", "Hostel 8 - Tapti"

    class Diet(models.TextChoices):
        VEG = "veg", "Vegetarian"
        NONVEG = "non-veg", "Non-vegetarian"
        JAIN = "jain", "Jain"
        EGG = "egg", "Eggetarian"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    student_code = models.CharField(
        max_length=16,
        unique=True,
        help_text="Pseudonymous ID used in analytics. Never show names to managers.",
    )
    hostel = models.CharField(
        max_length=8, choices=Hostel.choices, null=True, blank=True, default=None
    )
    diet = models.CharField(max_length=12, choices=Diet.choices, default=Diet.VEG)
    allergies = models.CharField(
        max_length=240,
        blank=True,
        help_text="Free text, e.g. 'peanuts, lactose'. Used for dish warnings only.",
    )
    default_plan = models.JSONField(default=empty_default_plan, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["student_code"]
        indexes = [
            models.Index(fields=["hostel"], name="profile_hostel_idx"),
            models.Index(fields=["diet"], name="profile_diet_idx"),
        ]

    def __str__(self) -> str:
        hostel = self.get_hostel_display() if self.hostel else "Day scholar"
        return f"{self.student_code} ({hostel})"

    def clean(self) -> None:
        validate_default_plan(self.default_plan)

    def save(self, *args, **kwargs):
        validate = kwargs.pop("validate", False)
        if validate:
            self.full_clean()
        super().save(*args, **kwargs)

    def normalized_plan(self) -> dict[str, dict[str, str]]:
        plan = empty_default_plan()
        if isinstance(self.default_plan, dict):
            for day, meals in self.default_plan.items():
                if str(day) not in plan or not isinstance(meals, dict):
                    continue
                for meal, value in meals.items():
                    if meal in MEAL_KEYS and value in PLAN_VALUES:
                        plan[str(day)][meal] = value
        return plan

    def plan_for(self, day_index: int, meal: str) -> str:
        return self.normalized_plan().get(str(day_index), {}).get(meal, "auto")


class Profile(models.Model):
    """Role and contact profile for every MessLab account.

    The custom user model is intentionally kept in place; this one-to-one
    profile is the least-privilege source of truth for role-based access.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="role_profile")
    role = models.CharField(max_length=16, choices=User.Role.choices, default=User.Role.STUDENT)
    phone = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        help_text="E.164 or local mobile number used for OTP login.",
    )
    hostel = models.CharField(
        max_length=8,
        choices=StudentProfile.Hostel.choices,
        null=True,
        blank=True,
        default=None,
    )
    is_pending = models.BooleanField(
        default=False,
        help_text="Self-registered but email not verified yet. Cannot sign in.",
    )
    data_consent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the member accepted the privacy notice for data processing.",
    )
    data_consent_version = models.CharField(max_length=16, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]
        indexes = [models.Index(fields=["role"], name="profile_role_idx")]

    def __str__(self) -> str:
        return f"{self.user.username} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        old_role = None
        if self.pk:
            old_role = (
                Profile.objects.filter(pk=self.pk).values_list("role", flat=True).first()
            )
        super().save(*args, **kwargs)
        # Keep the legacy column in sync without triggering the post_save signal.
        User.objects.filter(pk=self.user_id).exclude(role=self.role).update(role=self.role)
        if old_role and old_role != self.role:
            # A role change invalidates every outstanding JWT immediately.
            User.objects.filter(pk=self.user_id).update(
                token_version=models.F("token_version") + 1
            )


class AccountAudit(models.Model):
    """Who changed which account, without copying personal data into the log."""

    class Action(models.TextChoices):
        REGISTER = "register", "Registered"
        ROLE_CHANGE = "role_change", "Role changed"
        ACTIVATE = "activate", "Activated"
        DEACTIVATE = "deactivate", "Deactivated"
        DISH_SAVE = "dish_save", "Dish saved"
        DISH_DELETE = "dish_delete", "Dish deleted"
        MENU_SAVE = "menu_save", "Menu saved"
        MENU_DELETE = "menu_delete", "Menu deleted"
        SLOT_SAVE = "slot_save", "Meal slot saved"
        WASTE_SAVE = "waste_save", "Waste recorded"
        SERVICE_CLOSE = "service_close", "Service day closed"

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_actions"
    )
    target = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_entries"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["-at"], name="account_audit_at_idx")]

    def __str__(self) -> str:
        return f"{self.action} target={self.target_id} at {self.at:%Y-%m-%d %H:%M}"


class OTPCode(models.Model):
    """One-time login code. Only an HMAC digest is stored, never the code."""

    class Purpose(models.TextChoices):
        LOGIN = "login", "Login"
        REGISTER = "register", "Registration"
        DELETE_ACCOUNT = "delete_account", "Account deletion"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otp_codes")
    code_hash = models.CharField(max_length=64)
    salt = models.CharField(max_length=32, default="")
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.LOGIN)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"], name="otp_user_created_idx")]

    def __str__(self) -> str:
        return f"OTP for {self.user_id} created {self.created_at:%Y-%m-%d %H:%M}"


class StaffFeedback(models.Model):
    """Voice of cooks, helpers, servers and cleaners. Not a complaint box."""

    class Kind(models.TextChoices):
        OPINION = "opinion", "Opinion"
        NEED = "need", "Need"
        ISSUE = "issue", "Issue"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SEEN = "seen", "Seen"
        RESOLVED = "resolved", "Resolved"

    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="staff_feedback")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OPINION)
    message = models.TextField(max_length=2000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    supervisor_note = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="feedback_status_created_idx"),
            models.Index(fields=["author", "-created_at"], name="feedback_author_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.author_id} {self.kind}: {self.message[:40]}"


class LoginEvent(models.Model):
    """Lightweight audit of authentication attempts (no biometric data, ever)."""

    class Event(models.TextChoices):
        OTP_REQUEST = "otp_request", "OTP requested"
        OTP_SUCCESS = "otp_success", "OTP verified"
        OTP_FAILURE = "otp_failure", "OTP failed"
        LOCKOUT = "lockout", "Locked out"
        PASSWORD = "password", "Password login"
        LOGOUT = "logout", "Logout"

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="login_events"
    )
    email_masked = models.CharField(max_length=254, blank=True)
    event = models.CharField(max_length=20, choices=Event.choices, default=Event.OTP_REQUEST)
    success = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True)
    role = models.CharField(max_length=16, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["-at"], name="login_event_at_idx")]
