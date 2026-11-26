"""Single source of truth for role-based access control.

Every endpoint and page asks ``user_can``; nothing outside this module should
compare role strings. Admin (and Django superusers) bypass every check.
"""
from __future__ import annotations

from .models import User

# Canonical permission names used by API views, web views and templates.
PERMISSIONS = frozenset(
    {
        "menu.view",
        "crowd.view",
        "intent.write",
        "plan.weekly",
        "qr.show",
        "rewards.own",
        "leaderboard.view",
        "checkin.scan",
        "checkin.manual",
        "prep.view",
        "live.view",
        "plan.view",
        "plan.approve",
        "plan.override",
        "waste.view",
        "impact.view",
        "model.manage",
        "import.csv",
        "feedback.create",
        "feedback.review",
        "waste.write",
        "menu.manage",
        "service.close",
        "users.view",
        "users.manage",
        "users.role",
        "audit.view",
        # Mess Committee SOP
        "sop.view",
        "complaint.submit",
        "complaint.manage",
        "preference.respond",
        "menu.cycle",
        "committee.manage",
    }
)

_ALL = frozenset(PERMISSIONS)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    User.Role.HOSTEL_STUDENT: frozenset(
        {
            "menu.view",
            "crowd.view",
            "intent.write",
            "plan.weekly",
            "qr.show",
            "rewards.own",
            "leaderboard.view",
            "sop.view",
            "complaint.submit",
            "preference.respond",
        }
    ),
    User.Role.STUDENT: frozenset(
        {
            "menu.view",
            "crowd.view",
            "intent.write",
            "qr.show",
            "rewards.own",
            "leaderboard.view",
            "sop.view",
            "complaint.submit",
            "preference.respond",
        }
    ),
    User.Role.FACULTY: frozenset(
        {
            "menu.view",
            "crowd.view",
            "intent.write",
            "qr.show",
            "rewards.own",
            "leaderboard.view",
            "sop.view",
            "complaint.submit",
            "preference.respond",
        }
    ),
    User.Role.WORKER: frozenset(
        {"menu.view", "crowd.view", "prep.view", "feedback.create", "sop.view"}
    ),
    User.Role.CANTEEN_STAFF: frozenset(
        {
            "menu.view",
            "crowd.view",
            "checkin.scan",
            "checkin.manual",
            "prep.view",
            "live.view",
            "feedback.create",
            "waste.write",
            "sop.view",
        }
    ),
    User.Role.SUPERVISOR: frozenset(
        {
            "menu.view",
            "crowd.view",
            "rewards.own",
            "leaderboard.view",
            "checkin.scan",
            "checkin.manual",
            "prep.view",
            "live.view",
            "plan.view",
            "waste.view",
            "feedback.review",
            "waste.write",
            "menu.manage",
            "users.view",
            "sop.view",
            "complaint.manage",
        }
    ),
    User.Role.MANAGER: frozenset(
        {
            "menu.view",
            "crowd.view",
            "rewards.own",
            "leaderboard.view",
            "checkin.manual",
            "prep.view",
            "live.view",
            "plan.view",
            "plan.approve",
            "plan.override",
            "waste.view",
            "impact.view",
            "model.manage",
            "import.csv",
            "feedback.review",
            "waste.write",
            "menu.manage",
            "service.close",
            "users.view",
            "users.manage",
            "audit.view",
            "sop.view",
            "complaint.manage",
            "menu.cycle",
            "committee.manage",
        }
    ),
    User.Role.ADMIN: _ALL,
}

# Landing page per role. Keep in sync with the role table in the spec/README.
LANDING_BY_ROLE = {
    User.Role.HOSTEL_STUDENT: "/student/",
    User.Role.STUDENT: "/student/",
    User.Role.FACULTY: "/student/",
    User.Role.WORKER: "/staff/",
    User.Role.CANTEEN_STAFF: "/staff/scan/",
    User.Role.SUPERVISOR: "/supervisor/",
    User.Role.MANAGER: "/manager/",
    User.Role.ADMIN: "/manager/",
}


def role_of(user) -> str:
    if not getattr(user, "is_authenticated", False):
        return ""
    if getattr(user, "is_superuser", False):
        return User.Role.ADMIN
    try:
        return user.effective_role
    except Exception:
        return getattr(user, "role", "") or ""


def user_can(user, perm: str) -> bool:
    if perm not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {perm}")
    role = role_of(user)
    if not role:
        return False
    if role == User.Role.ADMIN:
        return True
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def permissions_for(user) -> frozenset[str]:
    role = role_of(user)
    if not role:
        return frozenset()
    if role == User.Role.ADMIN:
        return _ALL
    return ROLE_PERMISSIONS.get(role, frozenset())


def landing_for(user) -> str:
    return LANDING_BY_ROLE.get(role_of(user), "/login/")


def template_permissions(user) -> dict[str, bool]:
    """Context-processor keys such as ``perms_plan_view`` for templates."""
    return {
        f"perms_{perm.replace('.', '_')}": True for perm in permissions_for(user)
    }
