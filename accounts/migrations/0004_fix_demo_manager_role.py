"""Keep the seeded demo manager in the manager role even if it is a superuser."""
from django.db import migrations


def fix_manager(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Profile = apps.get_model("accounts", "Profile")
    user = User.objects.filter(username="manager").first()
    if not user:
        return
    User.objects.filter(pk=user.pk).update(role="manager")
    Profile.objects.filter(user_id=user.pk).update(role="manager")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_backfill_profiles")]
    operations = [migrations.RunPython(fix_manager, noop)]
