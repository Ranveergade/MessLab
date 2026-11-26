"""Menu catalogue and daily-menu editing shared by the web UI and the API."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q

from .models import DailyMenu, Dish, MenuItem


class MenuError(ValueError):
    """A menu change that cannot be applied."""


def dish_name_exists(name: str, *, exclude_pk: int | None = None) -> bool:
    qs = Dish.objects.filter(name__iexact=name.strip())
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _audit(actor, action: str, detail: dict, ip_address: str | None = None) -> None:
    """Write a menu change to the account audit trail.

    Menu changes are not account changes, so ``target`` stays empty; the actor,
    action and detail are enough to answer "who changed what, and when".
    """
    from accounts.models import AccountAudit

    AccountAudit.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        target=None,
        action=action,
        detail=detail,
        ip_address=ip_address or None,
    )


def save_dish(
    data: dict,
    *,
    instance: Dish | None = None,
    actor=None,
    ip_address: str | None = None,
) -> Dish:
    name = (data.get("name") or (instance.name if instance else "")).strip()
    if not name:
        raise MenuError("Dish name is required.")
    if dish_name_exists(name, exclude_pk=instance.pk if instance else None):
        raise MenuError("A dish with that name already exists.")

    created = instance is None
    dish = instance or Dish()
    dish.name = name
    dish.category = data.get("category") or dish.category or Dish.Category.MAIN
    dish.diet_type = data.get("diet_type") or dish.diet_type or Dish.DietType.VEG
    try:
        dish.avg_portion_grams = int(data.get("avg_portion_grams") or 180)
    except (TypeError, ValueError):
        raise MenuError("Average portion must be a whole number of grams.")
    if not 20 <= dish.avg_portion_grams <= 900:
        raise MenuError("Average portion must be between 20 and 900 grams.")
    try:
        dish.popularity_baseline = float(data.get("popularity_baseline") or 0.5)
    except (TypeError, ValueError):
        raise MenuError("Popularity must be a number between 0 and 1.")
    if not 0 <= dish.popularity_baseline <= 1:
        raise MenuError("Popularity must be between 0 and 1.")
    dish.is_active = bool(data.get("is_active", dish.is_active))
    dish.save()
    _audit(
        actor,
        "dish_save",
        {
            "change": "create" if created else "update",
            "dish_id": dish.pk,
            "name": dish.name,
            "category": dish.category,
            "diet_type": dish.diet_type,
            "avg_portion_grams": dish.avg_portion_grams,
            "popularity_baseline": dish.popularity_baseline,
            "is_active": dish.is_active,
        },
        ip_address,
    )
    return dish


def toggle_dish(
    dish: Dish, *, actor=None, ip_address: str | None = None
) -> Dish:
    """Activate or deactivate a dish without deleting its history."""
    dish.is_active = not dish.is_active
    dish.save(update_fields=["is_active"])
    _audit(
        actor,
        "dish_save",
        {
            "change": "activate" if dish.is_active else "deactivate",
            "dish_id": dish.pk,
            "name": dish.name,
            "is_active": dish.is_active,
        },
        ip_address,
    )
    return dish


def delete_dish(dish: Dish, *, actor=None, ip_address: str | None = None) -> None:
    if dish.menu_items.exists():
        raise MenuError("This dish is used by a menu. Deactivate it instead of deleting.")
    detail = {"dish_id": dish.pk, "name": dish.name}
    dish.delete()
    _audit(actor, "dish_delete", detail, ip_address)


def delete_menu(menu: DailyMenu, *, actor=None, ip_address: str | None = None) -> None:
    detail = {
        "menu_id": menu.pk,
        "date": menu.date.isoformat(),
        "meal": menu.meal.name,
        "items": [
            {"dish_id": item.dish_id, "kg": str(item.planned_quantity_kg)}
            for item in menu.items.all()
        ],
    }
    menu.delete()
    _audit(actor, "menu_delete", detail, ip_address)


def parse_quantity(raw) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise MenuError("Planned quantity must be a number of kilograms.")
    if value < 0:
        raise MenuError("Planned quantity cannot be negative.")
    if value > Decimal("999999.99"):
        raise MenuError("Planned quantity is too large.")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _restore_rows(model, snapshot: list[dict], *, date, meal) -> None:
    """Undo forecast/plan changes made for meals other than the edited one."""
    pk_name = model._meta.pk.attname
    pks = [row[pk_name] for row in snapshot]
    model.objects.filter(date=date).exclude(meal=meal).exclude(pk__in=pks).delete()
    for row in snapshot:
        values = {key: value for key, value in row.items() if key != pk_name}
        model.objects.filter(pk=row[pk_name]).update(**values)


def refresh_meal_forecasts(menu: DailyMenu) -> None:
    """Regenerate forecasts for one date x meal and reopen approved plans.

    ``generate_forecasts`` works per day and walks every meal slot. We snapshot
    the other meals of that day and restore them afterwards so a menu edit only
    touches the meal the manager actually changed.
    """
    from dining.models import Forecast, PreparationPlan
    from forecasting.pipeline import generate_forecasts, load_artifact

    date, meal = menu.date, menu.meal
    plan_snapshot = list(
        PreparationPlan.objects.filter(date=date).exclude(meal=meal).values()
    )
    forecast_snapshot = list(
        Forecast.objects.filter(date=date).exclude(meal=meal).values()
    )

    generate_forecasts(
        start_date=date, days=1, artifact=load_artifact(), force=True
    )

    _restore_rows(PreparationPlan, plan_snapshot, date=date, meal=meal)
    _restore_rows(Forecast, forecast_snapshot, date=date, meal=meal)

    dish_ids = list(menu.items.values_list("dish_id", flat=True))
    PreparationPlan.objects.filter(date=date, meal=meal).exclude(
        dish_id__in=dish_ids
    ).delete()
    Forecast.objects.filter(date=date, meal=meal, dish__isnull=False).exclude(
        dish_id__in=dish_ids
    ).delete()

    # A regenerated plan is a draft again; drop the old approval metadata.
    PreparationPlan.objects.filter(date=date, meal=meal).filter(
        Q(approved_by__isnull=False)
        | Q(approved_at__isnull=False)
        | ~Q(override_reason="")
    ).update(
        status=PreparationPlan.Status.DRAFT,
        approved_by=None,
        approved_at=None,
        override_reason="",
    )


@transaction.atomic
def save_daily_menu(
    *,
    date,
    meal,
    items: list[tuple[Dish, Decimal | str, bool]],
    notes: str = "",
    is_special: bool = False,
    details: dict[int, dict] | None = None,
    actor=None,
    ip_address: str | None = None,
) -> DailyMenu:
    """Create or replace the dish lines of a date × meal menu.

    ``details`` optionally carries the SOP §5 classification per dish:
    ``{"serving_category": ..., "portion_grams": ..., "frequency_per_month": ...}``.
    """
    if not date or not meal:
        raise MenuError("Choose a date and a meal.")
    details = details or {}
    seen: set[int] = set()
    prepared: list[tuple[Dish, Decimal, bool, str, int | None, int | None]] = []
    for dish, raw_quantity, special in items:
        if dish.pk in seen:
            raise MenuError(f"{dish.name} appears twice.")
        seen.add(dish.pk)
        detail = details.get(dish.pk) or {}
        category = (
            detail.get("serving_category") or MenuItem.ServingCategory.UNLIMITED
        )
        if category not in MenuItem.ServingCategory.values:
            raise MenuError(f"Unknown serving category for {dish.name}.")
        portion = _parse_optional_int(
            detail.get("portion_grams"), dish, "portion size", 1, 2000
        )
        frequency = _parse_optional_int(
            detail.get("frequency_per_month"), dish, "frequency", 1, 31
        )
        if category in {MenuItem.ServingCategory.LIMITED} and portion is None:
            raise MenuError(
                f"{dish.name} is a limited-quantity item, so enter its portion in grams."
            )
        if category in {
            MenuItem.ServingCategory.SPECIAL,
            MenuItem.ServingCategory.LIMITED,
        } and frequency is None:
            raise MenuError(
                f"{dish.name} is a {category} item, so enter how many times it is served."
            )
        prepared.append(
            (
                dish,
                parse_quantity(raw_quantity),
                bool(special),
                category,
                portion,
                frequency,
            )
        )

    menu, _ = DailyMenu.objects.get_or_create(
        date=date, meal=meal, defaults={"notes": notes[:240], "is_special": is_special}
    )
    menu.notes = (notes or "")[:240]
    menu.is_special = bool(is_special)
    menu.save(update_fields=["notes", "is_special"])

    existing = {item.dish_id: item for item in menu.items.select_related("dish")}
    keep: set[int] = set()
    for dish, quantity, special, category, portion, frequency in prepared:
        item = existing.get(dish.pk)
        if item is None:
            MenuItem.objects.create(
                daily_menu=menu,
                dish=dish,
                planned_quantity_kg=quantity,
                is_special=special,
                serving_category=category,
                portion_grams=portion,
                frequency_per_month=frequency,
            )
        else:
            item.planned_quantity_kg = quantity
            item.is_special = special
            item.serving_category = category
            item.portion_grams = portion
            item.frequency_per_month = frequency
            item.save(
                update_fields=[
                    "planned_quantity_kg",
                    "is_special",
                    "serving_category",
                    "portion_grams",
                    "frequency_per_month",
                ]
            )
        keep.add(dish.pk)
    MenuItem.objects.filter(daily_menu=menu).exclude(dish_id__in=keep).delete()

    refresh_meal_forecasts(menu)

    _audit(
        actor,
        "menu_save",
        {
            "menu_id": menu.pk,
            "date": date.isoformat(),
            "meal": meal.name,
            "notes": menu.notes,
            "is_special": menu.is_special,
            "items": [
                {
                    "dish_id": dish.pk,
                    "dish": dish.name,
                    "kg": str(quantity),
                    "special": special,
                    "serving_category": category,
                    "portion_grams": portion,
                    "frequency_per_month": frequency,
                }
                for dish, quantity, special, category, portion, frequency in prepared
            ],
        },
        ip_address,
    )
    return menu


def _parse_optional_int(raw, dish: Dish, label: str, low: int, high: int) -> int | None:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise MenuError(f"{dish.name}: {label} must be a whole number.")
    if not low <= value <= high:
        raise MenuError(f"{dish.name}: {label} must be between {low} and {high}.")
    return value


def menu_queryset(*, start=None, end=None, meal=None, query: str = ""):
    qs = DailyMenu.objects.select_related("meal").prefetch_related("items__dish")
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    if meal:
        qs = qs.filter(meal=meal)
    if query:
        qs = qs.filter(
            Q(notes__icontains=query) | Q(items__dish__name__icontains=query)
        ).distinct()
    return qs.order_by("-date", "meal__order")
