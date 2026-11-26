from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    if hasattr(mapping, "get"):
        value = mapping.get(key)
        if value is None:
            value = mapping.get(str(key))
        return value
    try:
        return mapping[key]
    except (KeyError, IndexError, TypeError):
        return None


@register.filter
def money(value):
    try:
        return f"₹{float(value):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


@register.filter
def kg(value):
    try:
        return f"{float(value):,.1f} kg"
    except (TypeError, ValueError):
        return "0.0 kg"


@register.filter
def one_decimal(value):
    try:
        return f"{float(value):,.1f}"
    except (TypeError, ValueError):
        return "0.0"


@register.filter
def percent(value, total=None):
    try:
        if total in (None, "", 0):
            return f"{float(value):.0f}%"
        return f"{float(value) / float(total) * 100:.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "0%"


@register.filter
def ratio_pct(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


@register.filter
def risk_width(value):
    try:
        return max(3, min(100, float(value) * 100))
    except (TypeError, ValueError):
        return 3


@register.filter
def bar_width(value, maximum):
    try:
        maximum = float(maximum)
        if maximum <= 0:
            return 0
        return max(2, min(100, float(value) / maximum * 100))
    except (TypeError, ValueError):
        return 0


@register.filter
def level_class(level):
    return {"low": "good", "medium": "warn", "high": "bad"}.get(level, "neutral")


@register.filter
def level_label(level):
    return {"low": "Low", "medium": "Medium", "high": "High"}.get(level, str(level).title())


@register.filter
def meal_icon(name):
    return {
        "breakfast": "☀️",
        "lunch": "🍛",
        "snacks": "☕",
        "dinner": "🌙",
    }.get(name, "🍽️")


@register.filter
def weekday_name(index):
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
        int(index) % 7
    ]


@register.filter
def initials(value):
    return "".join(part[0] for part in str(value).split()[:2]).upper() or "ML"


@register.filter
def abs_value(value):
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return 0


@register.filter
def status_class(status):
    return {
        "draft": "warn",
        "approved": "good",
        "overridden": "info",
        "YES": "good",
        "NO": "bad",
        "MAYBE": "warn",
    }.get(status, "neutral")


@register.filter
def dict_values(mapping):
    return mapping.values() if hasattr(mapping, "values") else []


@register.filter
def split_csv(value):
    return [part.strip() for part in str(value).split(",") if part.strip()]
