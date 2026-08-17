"""
Gunicorn entrypoint: `gunicorn --workers 1 --threads 2 --bind 127.0.0.1:8000 wsgi:app`
See deploy/scholarsync-panel.service for the exact command used in production.
"""

from admin_panel.app import app  # noqa: F401
