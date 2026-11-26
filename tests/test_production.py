"""Phase 6: the production settings module is explicit and fails closed."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
PYTHON = sys.executable


def run_settings(code: str, **env_overrides) -> subprocess.CompletedProcess:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "messlab.settings_prod"}
    for key in (
        "DEBUG",
        "DATABASE_URL",
        "EMAIL_BACKEND",
        "MESSLAB_ALLOW_SQLITE",
        "MESSLAB_ALLOW_CONSOLE_EMAIL",
    ):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [PYTHON, "-c", code],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class ProductionSettingsTests(SimpleTestCase):
    def test_production_settings_load_with_required_values(self):
        result = run_settings(
            "import django; django.setup();"
            "from django.conf import settings;"
            "print(settings.DEBUG);"
            "print(settings.STORAGES['staticfiles']['BACKEND']);"
            "print('whitenoise.middleware.WhiteNoiseMiddleware' in settings.MIDDLEWARE);"
            "print(settings.MESSLAB['ALLOW_PASSWORD_LOGIN']);",
            SECRET_KEY="production-secret-value",
            ALLOWED_HOSTS="messlab.example",
            MESSLAB_ALLOW_SQLITE="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0], "False")
        self.assertEqual(
            lines[1], "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
        self.assertEqual(lines[2], "True")
        self.assertEqual(lines[3], "False")

    def test_production_settings_reject_the_development_key(self):
        result = run_settings(
            "import django; django.setup()",
            SECRET_KEY="dev-only-insecure-key-change-in-production",
            ALLOWED_HOSTS="messlab.example",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_production_settings_require_allowed_hosts(self):
        result = run_settings(
            "import django; django.setup()",
            SECRET_KEY="production-secret-value",
            ALLOWED_HOSTS="",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ALLOWED_HOSTS", result.stderr)

    def test_production_requires_an_explicit_database_choice(self):
        result = run_settings(
            "import django; django.setup()",
            SECRET_KEY="production-secret-value",
            ALLOWED_HOSTS="messlab.example",
            MESSLAB_ALLOW_SQLITE="0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL", result.stderr)

    def test_production_rejects_the_console_email_backend(self):
        result = run_settings(
            "import django; django.setup()",
            SECRET_KEY="production-secret-value",
            ALLOWED_HOSTS="messlab.example",
            MESSLAB_ALLOW_SQLITE="1",
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
            MESSLAB_ALLOW_CONSOLE_EMAIL="0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OTP email", result.stderr)

    def test_base_settings_serve_static_files_through_whitenoise(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        self.assertIn("django.contrib.staticfiles", settings.INSTALLED_APPS)
