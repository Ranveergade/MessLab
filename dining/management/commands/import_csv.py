from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from dining.importers import CsvImportError, import_csv_file


class Command(BaseCommand):
    help = "Import organiser CSVs: participation, menus, prepared quantities or wastage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind",
            required=True,
            choices=["participation", "menus", "prepared", "wastage"],
        )
        parser.add_argument("--file", required=True, help="Path to the CSV file.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--allow-partial",
            action="store_true",
            help="Keep valid rows and report invalid rows instead of rolling back.",
        )
        parser.add_argument("--user", default=None, help="Username recorded in the import audit.")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = path.read_text(encoding="latin-1")
        user = User.objects.filter(username=options["user"]).first() if options["user"] else None
        try:
            result = import_csv_file(
                kind=options["kind"],
                filename=path.name,
                content=content,
                user=user,
                dry_run=options["dry_run"],
                allow_partial=options["allow_partial"],
            )
        except CsvImportError as exc:
            self.stderr.write(self.style.ERROR(f"Import failed: {len(exc.errors)} error(s)."))
            for error in exc.errors:
                self.stderr.write(f"  - {error}")
            raise CommandError("No rows were written.") from exc
        prefix = "Dry run" if result.dry_run else "Imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix} {result.kind}: {result.created} created, {result.updated} updated, "
                f"{result.skipped} skipped."
            )
        )
        if result.errors:
            self.stdout.write(self.style.WARNING(f"{len(result.errors)} row error(s):"))
            for error in result.errors:
                self.stdout.write(f"  - {error}")
