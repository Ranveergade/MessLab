"""JWT authentication that also enforces the per-user token version."""
from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken


class JWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Document the bearer scheme so drf-spectacular stops guessing."""

    target_class = "accounts.authentication.TokenVersionJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class TokenVersionJWTAuthentication(JWTAuthentication):
    """Reject inactive users and tokens issued before the last role change."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if not user.is_active:
            raise AuthenticationFailed("Account disabled.")
        if validated_token.get("token_version") != user.token_version:
            raise AuthenticationFailed("Session expired. Please sign in again.")
        return user


def issue_tokens(user) -> dict[str, str]:
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.effective_role
    refresh["token_version"] = user.token_version
    access = refresh.access_token
    access["role"] = user.effective_role
    access["token_version"] = user.token_version
    return {"access": str(access), "refresh": str(refresh)}


def bump_token_version(user) -> None:
    from .models import User

    User.objects.filter(pk=user.pk).update(token_version=user.token_version + 1)
    user.refresh_from_db(fields=["token_version"])
