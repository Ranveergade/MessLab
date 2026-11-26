"""Delete short-lived personal traces once their retention window passes.

MessLab keeps login security events, hashed one-time codes and account audit
rows only long enough to investigate incidents. This command is safe to run
from cron and prints what it would remove unless ``--apply`` is passed.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from insights.privacy import purge_older_than

DEFAULT_RETENTION_DAYS = 180


class Command(BaseCommand):
    help = "Purge expired login events, OTP codes and account audit rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without this flag the command only reports.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            self.stderr.write(self.style.ERROR("--days must be at least 1."))
            return
        result = purge_older_than(days, dry_run=not options["apply"])
        mode = "Deleted" if options["apply"] else "Would delete"
        self.stdout.write(
            f"{mode} personal traces older than {days} days "
            f"(cutoff {result['cutoff']}):"
        )
        for key in ("login_events", "otp_codes", "account_audit"):
            label = key.replace("_", " ")
            self.stdout.write(f"  {label}: {result[key]}")
        if not options["apply"]:
            self.stdout.write("Dry run. Re-run with --apply to delete.")
