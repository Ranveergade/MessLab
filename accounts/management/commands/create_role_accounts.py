"""Create one ready-to-use account for every MessLab role.

This is a local-development helper, not part of the fresh-deployment path. It
is idempotent: running it again keeps existing accounts, repairs the role
profile, and never resets a changed password unless ``--reset-passwords`` is
passed.

Accounts use ``<role>@messlab.local`` so an email code printed in the server
terminal can be requested immediately. The documented demo password also works
while ``MESSLAB_ALLOW_PASSWORD_LOGIN=1``.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Profile, StudentProfile, User
from accounts.rbac import LANDING_BY_ROLE
from accounts.signals import STUDENT_ROLES

DEFAULT_PASSWORD = "MessLab@2026"
EMAIL_DOMAIN = "messlab.local"

# Order matches the role hierarchy in the README: students, staff, management.
ROLE_ACCOUNTS = tuple(User.Role.values)

STUDENT_CODE_PREFIX = {
    User.Role.HOSTEL_STUDENT: "H",
    User.Role.STUDENT: "S",
    User.Role.FACULTY: "F",
}


class Command(BaseCommand):
    help = "Create one development account for every MessLab role (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--role",
            action="append",
            choices=ROLE_ACCOUNTS,
            help="Only create or repair this role. Repeat the flag for several.",
        )
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help=f"Password for new accounts (default {DEFAULT_PASSWORD}).",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Reset existing accounts to the chosen password as well.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        roles = options["role"] or list(ROLE_ACCOUNTS)
        password = options["password"]
        reset = options["reset_passwords"]

        created_count = 0
        lines = []
        for role in roles:
            user, created = self._ensure_account(
                role, password=password, reset_password=reset
            )
            created_count += int(created)
            lines.append(
                (
                    "created" if created else "exists ",
                    role,
                    user.username,
                    user.email,
                    LANDING_BY_ROLE.get(role, "/login/"),
                )
            )

        width = max(len(label) for label in (User.Role(role).label for role in roles))
        for status, role, username, email, landing in lines:
            label = User.Role(role).label
            self.stdout.write(
                f"  {status}  {label:<{width}}  {username:<15}  {email:<28}  {landing}"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{created_count} created, {len(roles) - created_count} already present."
            )
        )
        self.stdout.write(
            "Password login: /login/password/ (needs MESSLAB_ALLOW_PASSWORD_LOGIN=1)."
        )
        self.stdout.write(
            "Email code login: /login/ - the code is printed in the server terminal."
        )

    def _ensure_account(self, role: str, *, password: str, reset_password: bool):
        email = f"{role}@{EMAIL_DOMAIN}"
        user = User.objects.filter(email__iexact=email).first()
        created = False

        if user is None:
            user = User.objects.filter(username=role).first()
            if user is not None and user.email and user.email.lower() != email:
                # Someone else already owns the conventional username. Do not
                # take over their account; use a suffix for the demo account.
                user = None

        if user is None:
            username = role
            suffix = 1
            while User.objects.filter(username__iexact=username).exists():
                suffix += 1
                username = f"{role}{suffix}"
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                role=role,
                is_active=True,
                is_staff=role == User.Role.ADMIN,
            )
            created = True

        updates = []
        if user.role != role:
            user.role = role
            updates.append("role")
        if not user.is_active:
            user.is_active = True
            updates.append("is_active")
        if role == User.Role.ADMIN and not user.is_staff:
            user.is_staff = True
            updates.append("is_staff")
        if not user.email:
            user.email = email
            updates.append("email")
        if reset_password and not created:
            user.set_password(password)
            updates.append("password")
        if updates:
            user.save(update_fields=sorted(set(updates)))

        profile, _ = Profile.objects.get_or_create(user=user)
        if profile.role != role or profile.is_pending:
            profile.role = role
            profile.is_pending = False
            profile.save()

        if role in STUDENT_ROLES and not StudentProfile.objects.filter(user=user).exists():
            StudentProfile.objects.create(
                user=user,
                student_code=f"{STUDENT_CODE_PREFIX[role]}-{user.pk:05d}",
                hostel=StudentProfile.Hostel.H1 if role == User.Role.HOSTEL_STUDENT else None,
            )

        return user, created
