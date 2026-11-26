"""CSV import validation, rollback and organiser-data mapping."""
from __future__ import annotations

import datetime as dt

from django.test import TestCase

from dining.importers import CsvImportError, import_csv_file
from dining.models import DataImport, MealIntent, MealService, WasteLog
from menu.models import DailyMenu, Dish, MenuItem
from tests.factories import BASE_DATE, make_dish, make_slots, make_student


class CsvValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.dish = make_dish("Dal Fry")

    def test_header_mismatch_is_rejected(self):
        content = "date,meal,people\n2026-03-02,lunch,100\n"
        with self.assertRaises(CsvImportError) as ctx:
            import_csv_file(kind="participation", filename="bad.csv", content=content)
        self.assertIn("Header mismatch", str(ctx.exception.errors[0]))

    def test_inconsistent_wastage_row_is_rejected_and_rolled_back(self):
        content = (
            "date,meal,dish,prepared_kg,consumed_kg,wasted_kg\n"
            "2026-03-02,lunch,Dal Fry,10,9,2\n"
        )
        with self.assertRaises(CsvImportError) as ctx:
            import_csv_file(kind="wastage", filename="bad.csv", content=content)
        self.assertTrue(any("exceeds" in e for e in ctx.exception.errors))
        self.assertEqual(WasteLog.objects.count(), 0)
        self.assertEqual(DataImport.objects.count(), 0)

    def test_bad_date_and_number_report_row_numbers(self):
        content = (
            "date,meal,actual_participants\n"
            "not-a-date,lunch,100\n"
            "2026-03-03,lunch,many\n"
        )
        with self.assertRaises(CsvImportError) as ctx:
            import_csv_file(kind="participation", filename="bad.csv", content=content)
        joined = " ".join(ctx.exception.errors)
        self.assertIn("Row 2", joined)
        self.assertIn("Row 3", joined)
        self.assertEqual(MealService.objects.count(), 0)

    def test_dry_run_validates_without_persisting(self):
        content = (
            "date,meal,dish,prepared_kg,consumed_kg,wasted_kg\n"
            "2026-03-02,lunch,Dal Fry,10,7,3\n"
        )
        result = import_csv_file(
            kind="wastage", filename="dry.csv", content=content, dry_run=True
        )
        self.assertEqual(result.created, 1)
        self.assertTrue(result.dry_run)
        self.assertEqual(WasteLog.objects.count(), 0)
        self.assertEqual(DataImport.objects.count(), 0)

    def test_valid_wastage_import_creates_rows_and_audit(self):
        content = (
            "date,meal,dish,prepared_kg,consumed_kg,wasted_kg,leftover_reusable\n"
            "2026-03-02,lunch,Dal Fry,10,7,3,yes\n"
        )
        result = import_csv_file(kind="wastage", filename="waste.csv", content=content)
        self.assertEqual(result.created, 1)
        log = WasteLog.objects.get()
        self.assertEqual(float(log.wasted_kg), 3.0)
        self.assertTrue(log.leftover_reusable)
        audit = DataImport.objects.get()
        self.assertEqual(audit.kind, "wastage")
        self.assertEqual(audit.rows_created, 1)

    def test_menu_import_creates_dishes_and_menu_items(self):
        content = (
            "date,meal,dish,planned_quantity_kg,category,diet_type,avg_portion_grams,popularity_baseline\n"
            "2026-03-02,lunch,New Paneer Dish,12.5,main,veg,200,0.7\n"
        )
        result = import_csv_file(kind="menus", filename="menu.csv", content=content)
        self.assertEqual(result.created, 1)
        dish = Dish.objects.get(name="New Paneer Dish")
        menu = DailyMenu.objects.get(date=BASE_DATE, meal=self.slots["lunch"])
        item = MenuItem.objects.get(daily_menu=menu, dish=dish)
        self.assertEqual(float(item.planned_quantity_kg), 12.5)

    def test_participation_import_aggregates_service_and_context(self):
        content = (
            "date,meal,actual_participants,declared_intents,weather,is_holiday\n"
            "2026-03-02,lunch,420,300,rain,no\n"
        )
        result = import_csv_file(
            kind="participation", filename="part.csv", content=content
        )
        self.assertEqual(result.created, 1)
        service = MealService.objects.get()
        self.assertEqual(service.actual_participants, 420)
        self.assertEqual(service.declared_intents, 300)
        self.assertEqual(service.weather, "rain")

    def test_participation_per_student_import(self):
        student = make_student("csv-student")
        content = (
            "date,meal,student_id,status,expected_arrival_window\n"
            f"2026-03-02,lunch,{student.profile.student_code},YES,early\n"
        )
        result = import_csv_file(
            kind="participation", filename="intents.csv", content=content
        )
        self.assertEqual(result.created, 1)
        intent = MealIntent.objects.get(student=student)
        self.assertEqual(intent.status, MealIntent.Status.YES)
        self.assertEqual(intent.expected_arrival_window, MealIntent.Arrival.EARLY)

    def test_unknown_student_is_reported(self):
        content = (
            "date,meal,student_id,status\n"
            "2026-03-02,lunch,S-99999,YES\n"
        )
        with self.assertRaises(CsvImportError) as ctx:
            import_csv_file(kind="participation", filename="intents.csv", content=content)
        self.assertIn("not found", " ".join(ctx.exception.errors))
