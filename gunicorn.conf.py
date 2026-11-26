"""Gunicorn configuration.

Start the app with::

    DJANGO_SETTINGS_MODULE=messlab.settings_prod \
        gunicorn messlab.wsgi:application --config gunicorn.conf.py

Tune workers/threads through environment variables. The defaults assume a small
single-campus deployment; raise ``GUNICORN_WORKERS`` as traffic grows.
"""
from __future__ import annotations

import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(
    os.environ.get("GUNICORN_WORKERS", str(min(4, multiprocessing.cpu_count() * 2 + 1)))
)
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Trust X-Forwarded-* only from the reverse proxy in front of Gunicorn.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
