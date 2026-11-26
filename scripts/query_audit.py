"""Print SQL query counts and duplicate patterns for the busiest pages.

Run it against a copy of real data before a release::

    python scripts/query_audit.py --user admin@mess.example.edu

It uses Django's in-process test client with ``force_login`` and never writes
business rows. A page that issues far more queries than it renders rows usually
has an N+1 that should be fixed in the view's ``select_related`` /
``prefetch_related``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django
from django.db import connection
from django.test.utils import CaptureQueriesContext

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "messlab.settings")
django.setup()

from django.test import Client  # noqa: E402

from accounts.models import User  # noqa: E402

PAGES = (
    ("student-home", "/student/"),
    ("student-checkin", "/student/checkin/"),
    ("manager-dashboard", "/manager/"),
    ("manager-plan", "/manager/plan/"),
    ("manager-live", "/manager/live/"),
    ("manager-waste", "/manager/waste/"),
    ("manager-menu-day", "/manager/menu/day/"),
    ("manager-data", "/manager/data/"),
)


def normalise(sql: str) -> str:
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    return " ".join(sql.split())[:140]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="Email or username to sign in as.")
    parser.add_argument("--all", action="store_true", help="Run every page.")
    args = parser.parse_args()

    if args.user:
        user = User.objects.filter(email__iexact=args.user).first() or User.objects.filter(
            username__iexact=args.user
        ).first()
        if user is None:
            raise SystemExit(f"No user matches {args.user!r}.")
    else:
        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if user is None:
            raise SystemExit("No users exist. Pass --user after creating an account.")

    client = Client()
    client.force_login(user)
    print(f"Auditing as {user.get_username()} (role={user.effective_role})")
    for name, path in PAGES:
        if not args.all and name.startswith("student") and user.effective_role not in {
            "hostel_student",
            "student",
            "faculty",
            "admin",
        }:
            continue
        if not args.all and name.startswith("manager") and user.effective_role not in {
            "manager",
            "admin",
        }:
            continue
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path)
        duplicates = Counter(normalise(query["sql"]) for query in captured.captured_queries)
        repeated = [(count, sql) for sql, count in duplicates.most_common(3) if count > 1]
        print(f"{name:22} {path:24} status={response.status_code} queries={len(captured)}")
        for count, sql in repeated:
            print(f"    x{count} {sql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
