"""Backfill the SOP serving category from the legacy ``is_special`` flag."""
from django.db import migrations


def forwards(apps, schema_editor):
    MenuItem = apps.get_model("menu", "MenuItem")
    MenuItem.objects.filter(
        is_special=True, serving_category="unlimited"
    ).update(serving_category="special")


def backwards(apps, schema_editor):
    MenuItem = apps.get_model("menu", "MenuItem")
    MenuItem.objects.filter(serving_category="special").update(
        serving_category="unlimited"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("menu", "0003_menuitem_frequency_per_month_menuitem_portion_grams_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
