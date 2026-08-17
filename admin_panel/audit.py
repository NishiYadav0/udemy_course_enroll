"""
admin_panel/audit.py
----------------------
Append-only trail of admin actions: who changed what, and when.

Never writes secret VALUES — only which key was touched — so this file is
safe to look at even though it lives next to a panel that edits credentials.
"""

import os
from datetime import datetime, timezone

AUDIT_LOG_NAME = "audit.log"


def _path(base_dir: str) -> str:
    return os.path.join(base_dir, AUDIT_LOG_NAME)


def record(base_dir: str, username: str, ip: str, action: str, detail: str = "") -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} | user={username} | ip={ip} | {action}"
    if detail:
        line += f" | {detail}"
    try:
        with open(_path(base_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # auditing must never break the action it's logging


def recent(base_dir: str, max_lines: int = 100) -> list[str]:
    path = _path(base_dir)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    return lines[-max_lines:][::-1]
