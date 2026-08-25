"""
admin_panel/set_password.py
------------------------------
Interactive first-time setup AND password-reset tool for the admin panel.

Run this on the VM (never over the panel itself — this is the recovery
path if you ever get locked out):

    cd /home/ubuntu/scholarsync
    source admin_panel/venv/bin/activate
    python admin_panel/set_password.py

Creates admin_panel/.env if it doesn't exist yet, or updates just the
username/password fields in place (backing up the old file first, same
pattern as the rest of this project).

This is the SSH-based alternative to the web-based /setup/claim route (see
app.py + templates/setup/claim.html) — same end state either way. Since
app.py now auto-creates a bootstrap admin_panel/.env with just a SECRET_KEY
the moment the panel first boots (env_editor.bootstrap_panel_env), the file
existing is no longer proof that an admin account has been set up — that's
why "first time" below checks for ADMIN_USERNAME specifically, not just
whether the file is there.
"""

import getpass
import os

from werkzeug.security import generate_password_hash

from env_editor import append_or_update, bootstrap_panel_env
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
PROJECT_ROOT = os.path.dirname(BASE_DIR)


def main():
    print("=" * 66)
    print("  ScholarSync Admin Panel — set_password.py")
    print("=" * 66)

    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    first_time = not existing.get("ADMIN_USERNAME")

    username = input("Admin username [admin]: ").strip() or "admin"
    while True:
        password = getpass.getpass("New password (min 12 chars): ")
        if len(password) < 12:
            print("  Too short — try again.")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("  Didn't match — try again.")
            continue
        break

    password_hash = generate_password_hash(password)

    bootstrap_panel_env(ENV_PATH)  # no-op if the file already exists

    updates = {"ADMIN_USERNAME": username, "ADMIN_PASSWORD_HASH": password_hash}
    if first_time:
        scholarsync_root = input(f"ScholarSync project root [{PROJECT_ROOT}]: ").strip() or PROJECT_ROOT
        updates["SCHOLARSYNC_ROOT"] = scholarsync_root
        updates["SERVICE_NAME"] = "scholarsync"

    result = append_or_update(ENV_PATH, updates)
    if first_time:
        print(f"\nSet up {ENV_PATH}")
    else:
        print(f"\nUpdated {ENV_PATH} (backup: {os.path.basename(result['backup_path'] or '-')})")

    print("\nDone. Restart the panel service for this to take effect:")
    print("    sudo systemctl restart scholarsync-panel")


if __name__ == "__main__":
    main()
