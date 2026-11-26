"""Phase 0 guard: every role × endpoint follows accounts.rbac.ROLE_PERMISSIONS."""
from __future__ import annotations

import os
import re
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase

from accounts.models import StudentProfile, User
from accounts.rbac import LANDING_BY_ROLE, ROLE_PERMISSIONS, landing_for, user_can
from tests.factories import BASE_DATE, make_slots, make_user, set_today

# (url, permission that gates it)
API_CASES = (
    ("/api/v1/my-plan/", "plan.weekly"),
    ("/api/v1/intents/", "intent.write"),
    ("/api/v1/meals/today/", "intent.write"),
    ("/api/v1/crowd/", "crowd.view"),
    ("/api/v1/leaderboard/", "leaderboard.view"),
    ("/api/v1/rewards/me/", "rewards.own"),
    ("/api/v1/checkin/pass/", "qr.show"),
    ("/api/v1/manager/preparation-plan/", "plan.view"),
    ("/api/v1/manager/live/", "live.view"),
    ("/api/v1/manager/waste/", "waste.view"),
    ("/api/v1/manager/impact/", "impact.view"),
    ("/api/v1/manager/menu-insights/", "menu.manage"),
    ("/api/v1/manager/peak/", "plan.view"),
    ("/api/v1/manager/model-health/", "model.manage"),
    ("/api/v1/manager/export/plan.csv", "plan.view"),
    ("/api/v1/manager/dishes/", "menu.manage"),
    ("/api/v1/manager/menus/", "menu.manage"),
    ("/api/v1/manager/users/", "users.view"),
    ("/api/v1/manager/audit/", "audit.view"),
)

PAGE_CASES = (
    ("/student/", "intent.write"),
    ("/student/plan/", "plan.weekly"),
    ("/student/checkin/", "qr.show"),
    ("/student/pass/qr.svg", "qr.show"),
    ("/student/rewards/", "rewards.own"),
    ("/staff/", "prep.view"),
    ("/staff/scan/", "checkin.scan"),
    ("/supervisor/", "feedback.review"),
    ("/manager/", "plan.approve"),
    ("/manager/plan/", "plan.view"),
    ("/manager/live/", "live.view"),
    ("/manager/waste/", "waste.view"),
    ("/manager/impact/", "impact.view"),
    ("/manager/menu/", "menu.manage"),
    ("/manager/menu/manage/", "menu.manage"),
    ("/manager/model/", "model.manage"),
    ("/manager/import/", "import.csv"),
    ("/manager/users/", "users.view"),
    ("/manager/audit/", "audit.view"),
    # Mess Committee SOP
    ("/sop/", "sop.view"),
    ("/student/complaints/", "complaint.submit"),
    ("/student/menu-preferences/", "preference.respond"),
    ("/manager/committee/", "committee.manage"),
    ("/manager/menu-cycle/", "menu.cycle"),
    ("/manager/complaints/", "complaint.manage"),
    ("/menu/board/", "menu.view"),
)


class RoleMatrixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_slots()
        set_today(BASE_DATE)
        cls.users = {}
        for index, role in enumerate(User.Role.values):
            hostel = "H1" if role == User.Role.HOSTEL_STUDENT else None
            cls.users[role] = make_user(f"matrix-{role}", role, hostel=hostel)
        # Admin has every permission; give it a dining profile so the student
        # pages have data to render instead of failing for a non-permission reason.
        StudentProfile.objects.create(
            user=cls.users[User.Role.ADMIN], student_code="A-00001", hostel=None
        )

    def test_every_api_endpoint_matches_the_permission_matrix(self):
        for role, user in self.users.items():
            for url, perm in API_CASES:
                with self.subTest(role=role, url=url):
                    self.client.force_login(user)
                    response = self.client.get(url)
                    allowed = user_can(user, perm)
                    self.assertEqual(
                        response.status_code,
                        200 if allowed else 403,
                        msg=f"{role} on {url} (perm {perm})",
                    )

    def test_every_page_matches_the_permission_matrix(self):
        for role, user in self.users.items():
            for url, perm in PAGE_CASES:
                with self.subTest(role=role, url=url):
                    self.client.force_login(user)
                    response = self.client.get(url)
                    allowed = user_can(user, perm)
                    self.assertEqual(
                        response.status_code,
                        200 if allowed else 403,
                        msg=f"{role} on {url} (perm {perm})",
                    )

    def test_anonymous_api_is_rejected(self):
        client = Client()
        for url, _perm in API_CASES:
            with self.subTest(url=url):
                self.assertIn(client.get(url).status_code, (401, 403))

    def test_anonymous_page_redirects_to_login(self):
        client = Client()
        for url, _perm in PAGE_CASES:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])

    def test_landing_page_per_role(self):
        for role, user in self.users.items():
            self.assertEqual(landing_for(user), LANDING_BY_ROLE[role])

    def test_profile_role_is_the_source_of_truth(self):
        user = self.users[User.Role.STUDENT]
        self.assertFalse(user_can(user, "plan.view"))
        user.role_profile.role = User.Role.SUPERVISOR
        user.role_profile.save()
        user.refresh_from_db()
        self.assertEqual(user.effective_role, User.Role.SUPERVISOR)
        self.assertTrue(user_can(user, "plan.view"))

    def test_admin_permission_set_is_everything(self):
        from accounts.rbac import PERMISSIONS

        self.assertEqual(ROLE_PERMISSIONS[User.Role.ADMIN], frozenset(PERMISSIONS))

    def test_no_role_comparisons_outside_accounts(self):
        root = Path(settings.BASE_DIR)
        pattern = re.compile(r"\.role\s*==")
        skip_dirs = {
            ".venv",
            "ml_store",
            "accounts",
            "__pycache__",
            "staticfiles",
            "media",
            "node_modules",
        }
        offenders = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix not in {".py", ".html"} or name == "test_rbac.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(text):
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"Role comparisons found: {offenders}")
