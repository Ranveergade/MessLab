"""Validated CSV import so the data layer can be swapped for organiser data.

The importer is deliberately strict: every row is checked, errors are reported
with line numbers, and a failed import rolls back completely unless partial
imports are explicitly allowed.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounts.models import StudentProfile
from menu.models import DailyMenu, Dish, MealSlot, MenuItem

from .models import DataImport, DayContext, MealIntent, MealService, PreparationPlan, WasteLog

PARTICIPATION_AGG_HEADERS = {"date", "meal", "actual_participants"}
PARTICIPATION_STUDENT_HEADERS = {"date", "meal", "student_id", "status"}
MENU_HEADERS = {"date", "meal", "dish", "planned_quantity_kg"}
PREPARED_HEADERS = {"date", "meal", "dish", "prepared_kg"}
WASTAGE_HEADERS = {"date", "meal", "dish", "prepared_kg", "consumed_kg", "wasted_kg"}

# Keep one upload bounded so a malformed or hostile file cannot exhaust memory.
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 20_000

KIND_HEADERS = {
    "participation": [PARTICIPATION_AGG_HEADERS, PARTICIPATION_STUDENT_HEADERS],
    "menus": [MENU_HEADERS],
    "prepared": [PREPARED_HEADERS],
    "wastage": [WASTAGE_HEADERS],
}


class CsvImportError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"CSV import failed with {len(errors)} error(s).")


@dataclass
class ImportResult:
    kind: str
    filename: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "filename": self.filename,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def _clean_headers(fieldnames: list[str] | None) -> list[str]:
    if not fieldnames:
        return []
    return [f.replace("\ufeff", "").strip().lower().replace(" ", "_") for f in fieldnames]


def _parse_date(value: str, line: int, errors: list[str]) -> dt.date | None:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    errors.append(f"Row {line}: date {raw!r} is not ISO (YYYY-MM-DD) or DD/MM/YYYY.")
    return None


def _parse_float(value: str, field_name: str, line: int, errors: list[str], minimum=0.0) -> float | None:
    raw = (value or "").strip()
    try:
        number = float(raw)
    except (TypeError, ValueError):
        errors.append(f"Row {line}: {field_name} {raw!r} is not a number.")
        return None
    if math.isnan(number) or math.isinf(number):
        errors.append(f"Row {line}: {field_name} must be finite.")
        return None
    if number < minimum:
        errors.append(f"Row {line}: {field_name} must be >= {minimum}.")
        return None
    return number


def _parse_int(value: str, field_name: str, line: int, errors: list[str]) -> int | None:
    number = _parse_float(value, field_name, line, errors)
    if number is None:
        return None
    if int(number) != number:
        errors.append(f"Row {line}: {field_name} must be a whole number.")
        return None
    return int(number)


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "t"}


def _meal(value: str, line: int, errors: list[str]) -> MealSlot | None:
    name = (value or "").strip().lower()
    slot = MealSlot.objects.filter(name=name).first()
    if not slot:
        valid = ", ".join(MealSlot.objects.values_list("name", flat=True)) or "breakfast, lunch, snacks, dinner"
        errors.append(f"Row {line}: unknown meal {value!r}. Expected one of: {valid}.")
    return slot


def _dish(value: str, line: int, errors: list[str], create: bool = False) -> Dish | None:
    name = (value or "").strip()
    if not name:
        errors.append(f"Row {line}: dish name is required.")
        return None
    dish = Dish.objects.filter(name__iexact=name).first()
    if not dish and not create:
        errors.append(f"Row {line}: dish {name!r} does not exist. Import the menu first.")
    return dish


def _student(value: str, line: int, errors: list[str]) -> StudentProfile | None:
    code = (value or "").strip()
    if not code:
        errors.append(f"Row {line}: student_id is required.")
        return None
    profile = (
        StudentProfile.objects.select_related("user")
        .filter(student_code__iexact=code)
        .first()
    )
    if not profile:
        profile = StudentProfile.objects.select_related("user").filter(user__username__iexact=code).first()
    if not profile:
        errors.append(f"Row {line}: student {code!r} not found (use student_code or username).")
    return profile


def _read_rows(content: str) -> tuple[list[dict], list[str], list[str]]:
    stream = io.StringIO(content.lstrip("\ufeff"))
    reader = csv.DictReader(stream)
    headers = _clean_headers(reader.fieldnames)
    rows = []
    for raw in reader:
        cleaned = {}
        for key, value in raw.items():
            if key is None:
                continue
            cleaned[key.replace("\ufeff", "").strip().lower().replace(" ", "_")] = (
                value.strip() if isinstance(value, str) else value
            )
        rows.append(cleaned)
    return rows, headers, []


def _validate_headers(kind: str, headers: list[str]) -> None:
    valid_sets = KIND_HEADERS[kind]
    if not any(required.issubset(set(headers)) for required in valid_sets):
        expected = " OR ".join(sorted(", ".join(sorted(r)) for r in valid_sets))
        raise CsvImportError(
            [
                f"Header mismatch for {kind}. Found: {', '.join(headers) or '(none)'}. "
                f"Required: {expected}."
            ]
        )


def _import_participation(rows: list[dict], result: ImportResult) -> None:
    headers = set(rows[0].keys()) if rows else set()
    per_student = PARTICIPATION_STUDENT_HEADERS.issubset(headers)
    for line, row in enumerate(rows, start=2):
        errors: list[str] = []
        date = _parse_date(row.get("date", ""), line, errors)
        slot = _meal(row.get("meal", ""), line, errors)
        if errors:
            result.errors.extend(errors)
            continue
        assert date and slot
        if per_student:
            profile = _student(row.get("student_id", ""), line, errors)
            status = (row.get("status") or "").strip().upper()
            if status not in MealIntent.Status.values:
                errors.append(f"Row {line}: status must be YES, NO or MAYBE.")
            arrival = (row.get("expected_arrival_window") or "peak").strip().lower()
            if arrival not in MealIntent.Arrival.values:
                errors.append(f"Row {line}: expected_arrival_window must be early, peak or late.")
            if errors:
                result.errors.extend(errors)
                continue
            obj, created = MealIntent.objects.update_or_create(
                student=profile.user,
                date=date,
                meal=slot,
                defaults={
                    "status": status,
                    "source": MealIntent.Source.ONE_TAP,
                    "expected_arrival_window": arrival,
                },
            )
            result.created += 1 if created else 0
            result.updated += 0 if created else 1
        else:
            actual = _parse_int(row.get("actual_participants", ""), "actual_participants", line, errors)
            declared = _parse_int(
                row.get("declared_intents", "0") or "0", "declared_intents", line, errors
            )
            if errors:
                result.errors.extend(errors)
                continue
            assert actual is not None
            service, created = MealService.objects.update_or_create(
                date=date,
                meal=slot,
                defaults={
                    "actual_participants": actual,
                    "declared_intents": declared or 0,
                    "weather": (row.get("weather") or "clear").strip().lower(),
                    "is_holiday": _parse_bool(row.get("is_holiday", "")),
                    "is_exam": _parse_bool(row.get("is_exam", "")),
                    "is_special_menu": _parse_bool(row.get("is_special_menu", "")),
                    "event_name": (row.get("event_name") or "").strip(),
                    "notes": (row.get("notes") or "").strip(),
                },
            )
            ctx, _ = DayContext.objects.get_or_create(date=date)
            ctx.is_holiday = service.is_holiday
            ctx.is_exam = service.is_exam
            ctx.weather = service.weather
            ctx.event_name = service.event_name
            ctx.is_special_menu = service.is_special_menu
            ctx.save()
            result.created += 1 if created else 0
            result.updated += 0 if created else 1


def _import_menus(rows: list[dict], result: ImportResult) -> None:
    for line, row in enumerate(rows, start=2):
        errors: list[str] = []
        date = _parse_date(row.get("date", ""), line, errors)
        slot = _meal(row.get("meal", ""), line, errors)
        name = (row.get("dish") or "").strip()
        planned = _parse_float(
            row.get("planned_quantity_kg", "0") or "0", "planned_quantity_kg", line, errors
        )
        category = (row.get("category") or "main").strip().lower()
        diet_type = (row.get("diet_type") or "veg").strip().lower()
        if category not in Dish.Category.values:
            errors.append(f"Row {line}: category must be one of {', '.join(Dish.Category.values)}.")
        if diet_type not in Dish.DietType.values:
            errors.append(f"Row {line}: diet_type must be one of {', '.join(Dish.DietType.values)}.")
        if not name:
            errors.append(f"Row {line}: dish name is required.")
        if errors:
            result.errors.extend(errors)
            continue
        assert date and slot and planned is not None
        dish, _ = Dish.objects.get_or_create(
            name=name,
            defaults={
                "category": category,
                "diet_type": diet_type,
                "avg_portion_grams": int(float(row.get("avg_portion_grams") or 180)),
                "popularity_baseline": min(1.0, max(0.0, float(row.get("popularity_baseline") or 0.5))),
            },
        )
        menu, _ = DailyMenu.objects.get_or_create(
            date=date,
            meal=slot,
            defaults={
                "is_special": _parse_bool(row.get("is_special", "")),
                "notes": (row.get("menu_notes") or "").strip(),
            },
        )
        item, created = MenuItem.objects.update_or_create(
            daily_menu=menu,
            dish=dish,
            defaults={
                "planned_quantity_kg": Decimal(str(planned)),
                "is_special": _parse_bool(row.get("is_special", "")),
            },
        )
        result.created += 1 if created else 0
        result.updated += 0 if created else 1


def _import_prepared(rows: list[dict], result: ImportResult) -> None:
    for line, row in enumerate(rows, start=2):
        errors: list[str] = []
        date = _parse_date(row.get("date", ""), line, errors)
        slot = _meal(row.get("meal", ""), line, errors)
        dish = _dish(row.get("dish", ""), line, errors)
        prepared = _parse_float(row.get("prepared_kg", ""), "prepared_kg", line, errors)
        if errors:
            result.errors.extend(errors)
            continue
        assert date and slot and dish and prepared is not None
        plan = PreparationPlan.objects.filter(date=date, meal=slot, dish=dish).first()
        if plan:
            plan.actual_prepared_kg = Decimal(str(prepared))
            plan.status = PreparationPlan.Status.APPROVED
            plan.save(update_fields=["actual_prepared_kg", "status", "updated_at"])
            result.updated += 1
        else:
            PreparationPlan.objects.create(
                date=date,
                meal=slot,
                dish=dish,
                recommended_kg=Decimal(str(prepared)),
                baseline_kg=Decimal(str(prepared)),
                actual_prepared_kg=Decimal(str(prepared)),
                status=PreparationPlan.Status.APPROVED,
            )
            result.created += 1
        log = WasteLog.objects.filter(date=date, meal=slot, dish=dish).first()
        if log:
            consumed = float(log.consumed_kg)
            if prepared < consumed:
                result.errors.append(
                    f"Row {line}: prepared_kg {prepared} is below already logged consumed_kg {consumed}."
                )
                continue
            log.prepared_kg = Decimal(str(prepared))
            log.wasted_kg = Decimal(str(round(max(0.0, prepared - consumed), 2)))
            log.save(update_fields=["prepared_kg", "wasted_kg", "updated_at"])


def _import_wastage(rows: list[dict], result: ImportResult) -> None:
    for line, row in enumerate(rows, start=2):
        errors: list[str] = []
        date = _parse_date(row.get("date", ""), line, errors)
        slot = _meal(row.get("meal", ""), line, errors)
        dish = _dish(row.get("dish", ""), line, errors)
        prepared = _parse_float(row.get("prepared_kg", ""), "prepared_kg", line, errors)
        consumed = _parse_float(row.get("consumed_kg", ""), "consumed_kg", line, errors)
        wasted = _parse_float(row.get("wasted_kg", ""), "wasted_kg", line, errors)
        if prepared is not None and consumed is not None and wasted is not None:
            if consumed > prepared:
                errors.append(f"Row {line}: consumed_kg {consumed} exceeds prepared_kg {prepared}.")
            if wasted > prepared:
                errors.append(f"Row {line}: wasted_kg {wasted} exceeds prepared_kg {prepared}.")
            if consumed + wasted > prepared + 0.011:
                errors.append(
                    f"Row {line}: consumed_kg + wasted_kg ({consumed + wasted:.2f}) "
                    f"exceeds prepared_kg ({prepared:.2f})."
                )
        if errors:
            result.errors.extend(errors)
            continue
        assert date and slot and dish and prepared is not None and consumed is not None and wasted is not None
        obj, created = WasteLog.objects.update_or_create(
            date=date,
            meal=slot,
            dish=dish,
            defaults={
                "prepared_kg": Decimal(str(prepared)),
                "consumed_kg": Decimal(str(consumed)),
                "wasted_kg": Decimal(str(wasted)),
                "leftover_reusable": _parse_bool(row.get("leftover_reusable", "")),
            },
        )
        result.created += 1 if created else 0
        result.updated += 0 if created else 1


def import_csv_file(
    kind: str,
    filename: str,
    content: str,
    user=None,
    dry_run: bool = False,
    allow_partial: bool = False,
) -> ImportResult:
    if kind not in KIND_HEADERS:
        raise CsvImportError([f"Unknown kind {kind!r}. Use one of {', '.join(KIND_HEADERS)}."])
    if not content.strip():
        raise CsvImportError(["The uploaded CSV is empty."])
    if len(content.encode("utf-8", "ignore")) > MAX_CSV_BYTES:
        raise CsvImportError(
            [f"The CSV is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB."]
        )
    rows, headers, _ = _read_rows(content)
    if len(rows) > MAX_CSV_ROWS:
        raise CsvImportError(
            [f"The CSV has more than {MAX_CSV_ROWS:,} data rows. Split it into batches."]
        )
    _validate_headers(kind, headers)
    if not rows:
        raise CsvImportError(["The CSV has headers but no data rows."])
    result = ImportResult(kind=kind, filename=filename, dry_run=dry_run)
    try:
        with transaction.atomic():
            if kind == "participation":
                _import_participation(rows, result)
            elif kind == "menus":
                _import_menus(rows, result)
            elif kind == "prepared":
                _import_prepared(rows, result)
            elif kind == "wastage":
                _import_wastage(rows, result)
            if result.errors and not allow_partial:
                raise CsvImportError(result.errors[:50])
            if dry_run:
                transaction.set_rollback(True)
            else:
                DataImport.objects.create(
                    kind=kind,
                    filename=filename[:255],
                    rows_created=result.created,
                    rows_updated=result.updated,
                    rows_skipped=result.skipped,
                    errors=result.errors[:50],
                    imported_by=user if getattr(user, "is_authenticated", False) else None,
                )
    except CsvImportError:
        raise
    return result
