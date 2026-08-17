"""ScholarSync Admin Panel — Flask app package.

Run from the ScholarSync project ROOT (not from inside this folder), e.g.:
    gunicorn --workers 1 --threads 2 --bind 127.0.0.1:8000 admin_panel.wsgi:app

This matters because app.py does `from admin_panel import env_editor, ...` —
those imports only resolve correctly with the project root on the path.
"""
