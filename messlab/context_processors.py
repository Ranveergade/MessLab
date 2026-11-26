"""Small template helpers shared by every page."""
from __future__ import annotations

from accounts.models import User
from accounts.rbac import landing_for, role_of, template_permissions


def messlab_context(request):
    user = getattr(request, "user", None)
    role = ""
    role_label = ""
    if user and user.is_authenticated:
        role = role_of(user)
        try:
            role_label = User.Role(role).label
        except ValueError:
            role_label = role.replace("_", " ").title()

    perms = template_permissions(user) if user and user.is_authenticated else {}
    return {
        "app_name": "MessLab",
        "current_role": role,
        "role_label": role_label,
        "landing_url": landing_for(user) if user and user.is_authenticated else "/login/",
        **perms,
    }
