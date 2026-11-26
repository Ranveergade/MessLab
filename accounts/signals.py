"""Create the role profile (and dining profile where relevant) for new users."""
from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, StudentProfile, User

STUDENT_ROLES = {User.Role.HOSTEL_STUDENT, User.Role.STUDENT, User.Role.FACULTY}


@receiver(post_save, sender=User)
def ensure_profiles(sender, instance: User, created: bool, **kwargs) -> None:
    if not created:
        return

    role = instance.role
    if instance.is_superuser and role == User.Role.STUDENT:
        # A plain createsuperuser becomes the institute admin. An explicitly
        # created manager who is also a superuser keeps the manager role.
        role = User.Role.ADMIN
    hostel = StudentProfile.Hostel.H1 if role == User.Role.HOSTEL_STUDENT else None
    profile, profile_created = Profile.objects.get_or_create(
        user=instance,
        defaults={
            "role": role,
            "hostel": hostel,
        },
    )
    if profile_created and profile.role != role:
        profile.role = role
        profile.save(update_fields=["role", "updated_at"])

    if role not in STUDENT_ROLES:
        return
    if StudentProfile.objects.filter(user=instance).exists():
        return
    prefix = {
        User.Role.HOSTEL_STUDENT: "H",
        User.Role.STUDENT: "S",
        User.Role.FACULTY: "F",
    }[role]
    StudentProfile.objects.create(
        user=instance,
        student_code=f"{prefix}-{instance.pk:05d}",
        hostel=hostel,
        diet=StudentProfile.Diet.VEG,
    )
