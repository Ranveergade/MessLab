"""Create the minimum records a fresh deployment needs.

This command deliberately creates no sample students, menus or history. It only
adds the four standard meal slots (if missing) and the first administrator named
by ``BOOTSTRAP_ADMIN_EMAIL``.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from menu.models import MealSlot

MEAL_SLOTS = (
    ("breakfast", "07:30", "09:30", "08:15", "08:45", 1),
    ("lunch", "12:00", "14:30", "13:00", "13:30", 2),
    ("snacks", "16:30", "18:00", "17:00", "17:20", 3),
    ("dinner", "19:30", "21:30", "20:00", "20:30", 4),
)


class Command(BaseCommand):
    help = "Create the four meal slots and the first administrator. No demo data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--promote-existing",
            action="store_true",
            help="Promote an existing account with BOOTSTRAP_ADMIN_EMAIL to admin.",
        )

    def handle(self, *args, **options):
        created_slots = []
        for name, start, end, peak_start, peak_end, order in MEAL_SLOTS:
            _, created = MealSlot.objects.get_or_create(
                name=name,
                defaults={
                    "start_time": start,
                    "end_time": end,
                    "peak_start": peak_start,
                    "peak_end": peak_end,
                    "counters": 3,
                    "counter_capacity_per_hour": 420,
                    "order": order,
                },
            )
            if created:
                created_slots.append(name)
        if created_slots:
            self.stdout.write(f"Created meal slots: {', '.join(created_slots)}")
        else:
            self.stdout.write("Meal slots already exist.")

        email = (os.environ.get("BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
        if not email:
            raise CommandError(
                "Set BOOTSTRAP_ADMIN_EMAIL to the first administrator's email address."
            )
        existing = User.objects.filter(email__iexact=email).first()
        if existing:
            if not options.get("promote_existing"):
                self.stdout.write(
                    f"An account already exists for {email}; leaving it unchanged. "
                    "Pass --promote-existing to make it the administrator."
                )
                return
            existing.role = User.Role.ADMIN
            existing.is_active = True
            existing.is_staff = True
            existing.is_superuser = True
            existing.save(update_fields=["role", "is_active", "is_staff", "is_superuser"])
            profile = getattr(existing, "role_profile", None)
            if profile is not None:
                profile.role = User.Role.ADMIN
                profile.is_pending = False
                profile.save(update_fields=["role", "is_pending"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Promoted {email} to administrator. Sign in with an email code."
                )
            )
            return

        username = email.split("@", 1)[0][:120] or "admin"
        base = username
        suffix = 1
        while User.objects.filter(username__iexact=username).exists():
            suffix += 1
            username = f"{base}{suffix}"[:150]

        admin = User.objects.create_user(
            username=username,
            email=email,
            password=None,
            role=User.Role.ADMIN,
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created administrator {email}. Sign in with an email code."
            )
        )
