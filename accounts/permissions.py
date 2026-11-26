"""DRF and web permission adapters around :mod:`accounts.rbac`."""
from __future__ import annotations

from functools import wraps
from urllib.parse import quote

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from rest_framework.permissions import BasePermission

from .rbac import role_of, user_can

__all__ = ["HasPerm", "perm_required", "role_of"]


class _PermBase(BasePermission):
    perm = ""
    message = "Your role does not allow this action."

    def has_permission(self, request, view):
        return user_can(request.user, self.perm)


def HasPerm(perm: str):
    """Return a DRF permission class for one canonical permission name."""
    return type(
        "HasPerm_" + perm.replace(".", "_"),
        (_PermBase,),
        {"perm": perm, "message": f"Permission required: {perm}."},
    )


def perm_required(perm: str):
    """Page decorator: anonymous -> login, authenticated but denied -> 403."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                next_url = quote(request.get_full_path(), safe="/")
                return redirect(f"/login/?next={next_url}")
            if not user_can(request.user, perm):
                return HttpResponseForbidden("Your role does not allow this page.")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
