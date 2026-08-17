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
"""

import getpass
import os
import secrets

from werkzeug.security import generate_password_hash

from env_editor import update_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
PROJECT_ROOT = os.path.dirname(BASE_DIR)


def main():
    print("=" * 66)
    print("  ScholarSync Admin Panel — set_password.py")
    print("=" * 66)

    first_time = not os.path.exists(ENV_PATH)

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

    if first_time:
        scholarsync_root = input(f"ScholarSync project root [{PROJECT_ROOT}]: ").strip() or PROJECT_ROOT
        secret_key = secrets.token_hex(32)
        with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "# admin_panel/.env — the PANEL's OWN config. Different file from\n"
                "# the bot's .env one directory up. Never commit this file.\n"
                f"SECRET_KEY={secret_key}\n"
                f"ADMIN_USERNAME={username}\n"
                f"ADMIN_PASSWORD_HASH={password_hash}\n"
                f"SCHOLARSYNC_ROOT={scholarsync_root}\n"
                "SERVICE_NAME=scholarsync\n"
                "SESSION_TIMEOUT_MINUTES=120\n"
                "# Set to false ONLY for local http testing before Nginx/HTTPS is set up.\n"
                "BEHIND_HTTPS=true\n"
            )
        print(f"\nCreated {ENV_PATH}")
    else:
        result = update_values(ENV_PATH, {"ADMIN_USERNAME": username, "ADMIN_PASSWORD_HASH": password_hash})
        print(f"\nUpdated {ENV_PATH} (backup: {os.path.basename(result['backup_path'] or '-')})")

    print("\nDone. Restart the panel service for this to take effect:")
    print("    sudo systemctl restart scholarsync-panel")


if __name__ == "__main__":
    main()
