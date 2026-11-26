"""The development helper that creates one account per role."""
from __future__ import annotations

import io

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Profile, StudentProfile, User
from accounts.rbac import LANDING_BY_ROLE

PASSWORD = "MessLab@2026"
STUDENT_ROLES = {
    User.Role.HOSTEL_STUDENT,
    User.Role.STUDENT,
    User.Role.FACULTY,
}


class CreateRoleAccountsTests(TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command("create_role_accounts", *args, stdout=out)
        return out.getvalue()

    def test_creates_one_account_per_role(self):
        self.run_command()

        self.assertEqual(User.objects.count(), len(User.Role.values))
        for role in User.Role.values:
            user = User.objects.get(username=role)
            self.assertEqual(user.role, role)
            self.assertEqual(user.effective_role, role)
            self.assertEqual(user.email, f"{role}@messlab.local")
            self.assertTrue(user.is_active)
            self.assertTrue(user.check_password(PASSWORD))
            self.assertEqual(user.is_staff, role == User.Role.ADMIN)
            self.assertFalse(user.is_superuser)
            self.assertEqual(user.role_profile.role, role)
            self.assertFalse(user.role_profile.is_pending)

    def test_student_roles_get_a_dining_profile(self):
        self.run_command()

        for role in STUDENT_ROLES:
            user = User.objects.get(username=role)
            profile = StudentProfile.objects.get(user=user)
            self.assertTrue(profile.student_code)
        self.assertEqual(StudentProfile.objects.count(), len(STUDENT_ROLES))

        hostel_student = User.objects.get(username=User.Role.HOSTEL_STUDENT)
        self.assertEqual(
            hostel_student.profile.hostel, StudentProfile.Hostel.H1
        )
        self.assertIsNone(User.objects.get(username=User.Role.STUDENT).profile.hostel)

    def test_every_role_has_a_landing_page(self):
        self.assertEqual(set(LANDING_BY_ROLE), set(User.Role.values))

    def test_second_run_is_idempotent(self):
        self.run_command()
        first_ids = dict(User.objects.values_list("username", "pk"))
        first_hashes = dict(User.objects.values_list("username", "password"))

        output = self.run_command()

        self.assertEqual(dict(User.objects.values_list("username", "pk")), first_ids)
        self.assertEqual(dict(User.objects.values_list("username", "password")), first_hashes)
        self.assertIn("0 created", output)
        self.assertEqual(Profile.objects.count(), len(User.Role.values))

    def test_existing_account_keeps_its_password(self):
        self.run_command()
        user = User.objects.get(username=User.Role.STUDENT)
        user.set_password("changed-locally")
        user.save(update_fields=["password"])

        self.run_command()

        user.refresh_from_db()
        self.assertTrue(user.check_password("changed-locally"))

    def test_reset_passwords_flag_restores_the_documented_password(self):
        self.run_command()
        user = User.objects.get(username=User.Role.STUDENT)
        user.set_password("changed-locally")
        user.save(update_fields=["password"])

        self.run_command("--reset-passwords")

        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_role_filter_only_touches_the_requested_role(self):
        output = self.run_command("--role", User.Role.MANAGER)

        self.assertEqual(list(User.objects.values_list("username", flat=True)), [User.Role.MANAGER])
        self.assertIn("Mess Manager", output)

    def test_existing_username_with_another_email_is_not_taken_over(self):
        other = User.objects.create_user(
            username=User.Role.WORKER,
            email="real-worker@example.com",
            password="keep-me",
            role=User.Role.STUDENT,
        )

        self.run_command("--role", User.Role.WORKER)

        other.refresh_from_db()
        self.assertEqual(other.email, "real-worker@example.com")
        self.assertEqual(other.effective_role, User.Role.STUDENT)
        demo = User.objects.get(email=f"{User.Role.WORKER}@messlab.local")
        self.assertEqual(demo.username, f"{User.Role.WORKER}2")
        self.assertEqual(demo.effective_role, User.Role.WORKER)
