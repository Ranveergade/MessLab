"""Back-fill a Profile for every existing user and map legacy roles.

Rules:
* superuser -> admin
* username "manager" -> manager
* otherwise keep a valid role, defaulting to student
"""
from django.db import migrations


VALID_ROLES = {
    "student",
    "faculty",
    "worker",
    "mess_staff",
    "supervisor",
    "manager",
    "admin",
}


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Profile = apps.get_model("accounts", "Profile")
    StudentProfile = apps.get_model("accounts", "StudentProfile")

    for user in User.objects.all().iterator():
        if user.username == "manager":
            role = "manager"
        elif user.is_superuser:
            role = "admin"
        elif user.role in VALID_ROLES:
            role = user.role
        else:
            role = "student"

        hostel = "H1"
        student_profile = StudentProfile.objects.filter(user_id=user.pk).first()
        if student_profile:
            hostel = student_profile.hostel or "H1"

        Profile.objects.update_or_create(
            user_id=user.pk,
            defaults={
                "role": role,
                "hostel": hostel,
                "phone": "",
                "preferred_language": "en",
            },
        )
        if user.role != role:
            User.objects.filter(pk=user.pk).update(role=role)


def unbackfill(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_alter_user_role_otpcode_profile_stafffeedback")]
    operations = [migrations.RunPython(backfill, unbackfill)]
