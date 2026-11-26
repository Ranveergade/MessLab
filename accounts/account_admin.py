"""Shared account-administration rules for the web UI and the API.

Keeping the guards here means the API and the HTML page cannot drift apart.
"""
from __future__ import annotations

from .authentication import bump_token_version
from .models import AccountAudit, Profile, StudentProfile, User
from .rbac import role_of
from .registration import next_student_code


class AccountAdminError(Exception):
    """A requested account change is not allowed."""


def target_code(user: User | None) -> str:
    if not user:
        return "—"
    try:
        return user.profile.student_code
    except StudentProfile.DoesNotExist:
        return f"U-{user.pk:05d}"


def can_manage_target(actor: User, target: User) -> bool:
    if actor.pk == target.pk:
        return False
    if target.is_superuser and not actor.is_superuser:
        return False
    if role_of(target) == User.Role.ADMIN and not actor.is_superuser:
        return False
    return True


def audit(
    actor: User, target: User, action: str, detail: dict, request=None
) -> AccountAudit:
    return AccountAudit.objects.create(
        actor=actor,
        target=target,
        action=action,
        detail=detail,
        ip_address=(request.META.get("REMOTE_ADDR") if request else None) or None,
    )


def change_role(actor: User, target: User, new_role: str, request=None) -> bool:
    """Change a role, keep profiles consistent and invalidate old tokens."""
    if new_role not in User.Role.values:
        raise AccountAdminError("Unknown role.")
    if not can_manage_target(actor, target):
        raise AccountAdminError("You cannot change this account.")
    if new_role == User.Role.ADMIN and not actor.is_superuser:
        raise AccountAdminError("Only a superuser can grant the administrator role.")

    old_role = role_of(target)
    if old_role == new_role:
        return False

    profile, _ = Profile.objects.get_or_create(user=target)
    profile.role = new_role
    if new_role != User.Role.HOSTEL_STUDENT:
        profile.hostel = None
    profile.save()

    if new_role in (User.Role.HOSTEL_STUDENT, User.Role.STUDENT, User.Role.FACULTY):
        StudentProfile.objects.get_or_create(
            user=target, defaults={"student_code": next_student_code(new_role)}
        )
    if new_role != User.Role.HOSTEL_STUDENT:
        try:
            student = target.profile
        except StudentProfile.DoesNotExist:
            student = None
        if student and student.hostel:
            student.hostel = None
            student.save(update_fields=["hostel"])

    audit(
        actor,
        target,
        AccountAudit.Action.ROLE_CHANGE,
        {"from": old_role, "to": new_role},
        request,
    )
    return True


def set_active(actor: User, target: User, make_active: bool, request=None) -> bool:
    if not can_manage_target(actor, target):
        raise AccountAdminError("You cannot change this account.")
    if target.is_active == make_active:
        return False
    target.is_active = make_active
    target.save(update_fields=["is_active"])
    bump_token_version(target)
    audit(
        actor,
        target,
        AccountAudit.Action.ACTIVATE
        if make_active
        else AccountAudit.Action.DEACTIVATE,
        {"active": make_active},
        request,
    )
    return True
