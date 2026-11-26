"""Phase 3: dish catalogue and daily-menu management (web + API)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from menu.models import DailyMenu, Dish, MenuItem
from menu.services import (
    MenuError,
    dish_name_exists,
    parse_quantity,
    save_daily_menu,
    save_dish,
)
from tests.factories import BASE_DATE, make_dish, make_manager, make_slots, make_student

MENU_DATE = BASE_DATE + dt.timedelta(days=1)


class MenuServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()

    def test_save_dish_creates_and_normalises_the_name(self):
        dish = save_dish(
            {
                "name": "  Rajma Masala  ",
                "category": Dish.Category.MAIN,
                "avg_portion_grams": "200",
                "popularity_baseline": "0.7",
            }
        )
        self.assertEqual(dish.name, "Rajma Masala")
        self.assertEqual(dish.avg_portion_grams, 200)
        self.assertAlmostEqual(dish.popularity_baseline, 0.7)

    def test_duplicate_names_are_rejected_case_insensitively(self):
        make_dish("Paneer Tikka")
        self.assertTrue(dish_name_exists("paneer tikka"))
        with self.assertRaises(MenuError):
            save_dish({"name": "PANEER TIKKA"})

    def test_a_dish_may_keep_its_own_name_on_update(self):
        dish = make_dish("Chole Bhature")
        updated = save_dish({"name": "Chole Bhature", "avg_portion_grams": 260}, instance=dish)
        self.assertEqual(updated.pk, dish.pk)
        self.assertEqual(updated.avg_portion_grams, 260)

    def test_partial_update_keeps_the_existing_name(self):
        dish = make_dish("Poha")
        updated = save_dish({"popularity_baseline": 0.4}, instance=dish)
        self.assertEqual(updated.name, "Poha")

    def test_portion_and_popularity_bounds(self):
        with self.assertRaises(MenuError):
            save_dish({"name": "Tiny", "avg_portion_grams": 5})
        with self.assertRaises(MenuError):
            save_dish({"name": "Sweet", "popularity_baseline": 1.5})
        with self.assertRaises(MenuError):
            save_dish({"name": "Jumbo", "avg_portion_grams": 1200})

    def test_parse_quantity_rejects_bad_values(self):
        self.assertEqual(parse_quantity("12.345"), Decimal("12.35"))
        for raw in ("-1", "abc", "", None, "1000000"):
            with self.subTest(raw=raw), self.assertRaises(MenuError):
                parse_quantity(raw)

    def test_save_daily_menu_creates_lines(self):
        paneer = make_dish("Paneer Butter Masala")
        dal = make_dish("Dal Tadka")
        menu = save_daily_menu(
            date=MENU_DATE,
            meal=self.slots["lunch"],
            items=[(paneer, "12.5", False), (dal, 8, True)],
            notes="Festival lunch",
            is_special=True,
        )
        self.assertEqual(menu.items.count(), 2)
        self.assertTrue(menu.is_special)
        self.assertEqual(menu.notes, "Festival lunch")
        dal_line = menu.items.get(dish=dal)
        self.assertTrue(dal_line.is_special)
        self.assertEqual(dal_line.planned_quantity_kg, Decimal("8.00"))

    def test_save_daily_menu_replaces_lines(self):
        paneer = make_dish("Paneer Butter Masala")
        dal = make_dish("Dal Tadka")
        save_daily_menu(
            date=MENU_DATE,
            meal=self.slots["lunch"],
            items=[(paneer, 10, False), (dal, 8, False)],
        )
        menu = save_daily_menu(
            date=MENU_DATE,
            meal=self.slots["lunch"],
            items=[(paneer, 14, False)],
        )
        self.assertEqual(DailyMenu.objects.count(), 1)
        self.assertEqual(menu.items.count(), 1)
        self.assertEqual(menu.items.get().planned_quantity_kg, Decimal("14.00"))

    def test_duplicate_dishes_in_one_submission_are_rejected(self):
        paneer = make_dish("Paneer Butter Masala")
        with self.assertRaises(MenuError):
            save_daily_menu(
                date=MENU_DATE,
                meal=self.slots["lunch"],
                items=[(paneer, 10, False), (paneer, 5, False)],
            )
        self.assertFalse(DailyMenu.objects.exists())

    def test_menu_requires_a_date_and_meal(self):
        with self.assertRaises(MenuError):
            save_daily_menu(date=None, meal=self.slots["lunch"], items=[])


class DishApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.manager = make_manager("menu-manager")
        cls.student = make_student("menu-student")

    def setUp(self):
        self.client.force_login(self.manager)

    def test_create_list_update_and_delete(self):
        created = self.client.post(
            "/api/v1/manager/dishes/",
            {
                "name": "Veg Biryani",
                "category": "rice",
                "diet_type": "veg",
                "avg_portion_grams": 300,
                "popularity_baseline": 0.8,
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        dish_id = created.json()["id"]

        listed = self.client.get("/api/v1/manager/dishes/?q=biryani")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)

        patched = self.client.patch(
            f"/api/v1/manager/dishes/{dish_id}/",
            {"popularity_baseline": 0.6},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["name"], "Veg Biryani")
        self.assertAlmostEqual(patched.json()["popularity_baseline"], 0.6)

        deleted = self.client.delete(f"/api/v1/manager/dishes/{dish_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Dish.objects.filter(pk=dish_id).exists())

    def test_duplicate_dish_name_is_a_400(self):
        make_dish("Masala Dosa")
        response = self.client.post(
            "/api/v1/manager/dishes/",
            {"name": "masala dosa"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_dish_used_by_a_menu_cannot_be_deleted(self):
        dish = make_dish("Idli")
        save_daily_menu(
            date=MENU_DATE, meal=self.slots["breakfast"], items=[(dish, 5, False)]
        )
        response = self.client.delete(f"/api/v1/manager/dishes/{dish.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Dish.objects.filter(pk=dish.pk).exists())

    def test_invalid_portion_is_rejected(self):
        response = self.client.post(
            "/api/v1/manager/dishes/",
            {"name": "Rocks", "avg_portion_grams": 2},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_students_cannot_manage_dishes(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get("/api/v1/manager/dishes/").status_code, 403)


class DailyMenuApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.manager = make_manager("menu-manager")
        cls.paneer = make_dish("Paneer Butter Masala")
        cls.dal = make_dish("Dal Tadka")

    def setUp(self):
        self.client.force_login(self.manager)

    def test_create_menu_and_replace_it(self):
        created = self.client.post(
            "/api/v1/manager/menus/",
            {
                "date": MENU_DATE.isoformat(),
                "meal": "lunch",
                "notes": "Welcome lunch",
                "items": [
                    {"dish": self.paneer.pk, "planned_quantity_kg": "12.50"},
                    {"dish": self.dal.pk, "planned_quantity_kg": "8.00", "is_special": True},
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        payload = created.json()
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["meal"]["name"], "lunch")
        menu_id = payload["id"]

        replaced = self.client.put(
            f"/api/v1/manager/menus/{menu_id}/",
            {
                "date": MENU_DATE.isoformat(),
                "meal": "lunch",
                "items": [{"dish": self.paneer.pk, "planned_quantity_kg": "20.00"}],
            },
            content_type="application/json",
        )
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(len(replaced.json()["items"]), 1)
        menu = DailyMenu.objects.get(pk=menu_id)
        self.assertEqual(menu.items.get().planned_quantity_kg, Decimal("20.00"))

    def test_list_filters_by_date_and_meal(self):
        save_daily_menu(
            date=MENU_DATE, meal=self.slots["lunch"], items=[(self.paneer, 10, False)]
        )
        save_daily_menu(
            date=MENU_DATE + dt.timedelta(days=1),
            meal=self.slots["dinner"],
            items=[(self.dal, 9, False)],
        )
        response = self.client.get(
            f"/api/v1/manager/menus/?start={MENU_DATE.isoformat()}"
            f"&end={MENU_DATE.isoformat()}&meal=lunch"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_duplicate_dish_lines_are_rejected(self):
        response = self.client.post(
            "/api/v1/manager/menus/",
            {
                "date": MENU_DATE.isoformat(),
                "meal": "lunch",
                "items": [
                    {"dish": self.paneer.pk, "planned_quantity_kg": "10.00"},
                    {"dish": self.paneer.pk, "planned_quantity_kg": "5.00"},
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DailyMenu.objects.exists())

    def test_negative_quantity_is_rejected(self):
        response = self.client.post(
            "/api/v1/manager/menus/",
            {
                "date": MENU_DATE.isoformat(),
                "meal": "lunch",
                "items": [{"dish": self.paneer.pk, "planned_quantity_kg": "-1"}],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_meal_is_rejected(self):
        response = self.client.post(
            "/api/v1/manager/menus/",
            {
                "date": MENU_DATE.isoformat(),
                "meal": "brunch",
                "items": [{"dish": self.paneer.pk, "planned_quantity_kg": "1"}],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_menu(self):
        menu = save_daily_menu(
            date=MENU_DATE, meal=self.slots["lunch"], items=[(self.paneer, 10, False)]
        )
        response = self.client.delete(f"/api/v1/manager/menus/{menu.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(DailyMenu.objects.filter(pk=menu.pk).exists())
        self.assertFalse(MenuItem.objects.exists())


class MenuWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()
        cls.manager = make_manager("web-menu-manager")
        cls.student = make_student("web-menu-student")
        cls.paneer = make_dish("Paneer Butter Masala", portion=200, popularity=0.9)
        cls.dal = make_dish("Dal Tadka", category=Dish.Category.SIDE)

    def test_page_renders_for_manager(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("web:manager-menu-manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Menu manager")
        self.assertContains(response, "Paneer Butter Masala")

    def test_page_is_forbidden_for_students(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("web:manager-menu-manage"))
        self.assertEqual(response.status_code, 403)

    def test_add_dish_from_the_page(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-menu-manage"),
            {
                "action": "dish-save",
                "name": "Lemon Rice",
                "category": Dish.Category.RICE,
                "diet_type": Dish.DietType.VEG,
                "avg_portion_grams": 220,
                "popularity_baseline": 0.6,
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Dish.objects.filter(name="Lemon Rice").exists())

    def test_duplicate_dish_on_the_page_shows_an_error(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-menu-manage"),
            {
                "action": "dish-save",
                "name": "dal tadka",
                "category": Dish.Category.SIDE,
                "diet_type": Dish.DietType.VEG,
                "avg_portion_grams": 180,
                "popularity_baseline": 0.5,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(Dish.objects.filter(name__iexact="dal tadka").count(), 1)

    def test_publish_a_menu_from_the_page(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-menu-manage"),
            {
                "action": "menu-save",
                "date": MENU_DATE.isoformat(),
                "meal": self.slots["lunch"].pk,
                "notes": "Guest meal",
                "is_special": "on",
                f"qty_{self.paneer.pk}": "15.5",
                f"qty_{self.dal.pk}": "9",
                f"special_{self.dal.pk}": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        menu = DailyMenu.objects.get(date=MENU_DATE, meal=self.slots["lunch"])
        self.assertTrue(menu.is_special)
        self.assertEqual(menu.notes, "Guest meal")
        self.assertEqual(menu.items.count(), 2)
        self.assertTrue(menu.items.get(dish=self.dal).is_special)

    def test_bad_quantity_keeps_the_menu_unchanged(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-menu-manage"),
            {
                "action": "menu-save",
                "date": MENU_DATE.isoformat(),
                "meal": self.slots["lunch"].pk,
                f"qty_{self.paneer.pk}": "-4",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be negative")
        self.assertFalse(DailyMenu.objects.exists())

    def test_deactivate_a_dish(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("web:manager-menu-manage"),
            {"action": "dish-toggle", "dish_id": self.dal.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.dal.refresh_from_db()
        self.assertFalse(self.dal.is_active)
