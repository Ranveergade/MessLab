"""Liveness and readiness probes for container platforms."""
from __future__ import annotations

import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def healthz(request):
    """The process is alive. Does not touch the database."""
    return JsonResponse({"status": "ok"})


def readyz(request):
    """The process can serve real traffic: database reachable."""
    try:
        connection.ensure_connection()
    except Exception:
        logger.exception("Readiness check failed")
        return JsonResponse(
            {"status": "error", "database": "unavailable"}, status=503
        )
    return JsonResponse({"status": "ok", "database": "ok"})
