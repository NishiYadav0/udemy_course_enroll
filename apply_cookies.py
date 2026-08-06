"""
apply_cookies.py
----------------
Reads a cookies text file exported from Chrome DevTools and writes the values
into your .env file automatically — so you never have to hand-paste five long
cookie strings into WinSCP (which is exactly where typos creep in).

Nothing is sent anywhere. This runs entirely on your machine / your VM and
only rewrites your local .env.

USAGE
-----
    python apply_cookies.py "six cookies.txt"

    # to also set the User-Agent at the same time (recommended):
    python apply_cookies.py "six cookies.txt" --ua "<paste navigator.userAgent>"

ACCEPTED FILE FORMATS (all handled automatically)
------------------------------------------------
    access_token                 <- name on one line, value on the next
                                    (blank lines between are fine)
    "AbCdEf123...:XyZ789..."

    access_token=AbCdEf123...    <- or name=value
    access_token: AbCdEf123...   <- or name: value
    access_token	AbCdEf123... <- or name<TAB>value  (DevTools copy/paste)

Surrounding double quotes are stripped automatically.
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

# Cookie name (as shown in DevTools)  ->  .env variable name
COOKIE_TO_ENV: dict[str, str] = {
    "access_token":   "UDEMY_ACCESS_TOKEN",
    "csrftoken":      "UDEMY_CSRF_TOKEN",
    "dj_session_id":  "UDEMY_DJ_SESSION_ID",
    "ud_user_jwt":    "UDEMY_USER_JWT",
    "client_id":      "UDEMY_CLIENT_ID",
    "cf_clearance":   "UDEMY_CF_CLEARANCE",
    "__cf_bm":        "UDEMY_CF_BM",
    "ud_cache_user":  "UDEMY_USER_ID",
}

# Cookies that must be present for enrollment to have any chance of working.
REQUIRED = ["access_token", "csrftoken", "dj_session_id", "ud_user_jwt", "cf_clearance"]


def mask(value: str) -> str:
    """Show only enough to confirm it landed — never the whole secret."""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-3:]}  (len {len(value)})"


def parse_cookie_file(path: str) -> dict[str, str]:
    """
    Parse a cookie dump in any of the supported layouts.

    Strategy: first try to find 'name<sep>value' on a single line. Anything
    that's a bare known cookie name on its own line is treated as a header
    whose value is the next non-empty line.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw_lines = [ln.strip().strip("﻿") for ln in fh]

    lines = [ln for ln in raw_lines if ln.strip()]
    found: dict[str, str] = {}
    pending_name: str | None = None

    for line in lines:
        line = line.strip()

        # Case 1: "name=value" / "name: value" / "name<TAB>value"
        m = re.match(r"^(__cf_bm|[A-Za-z_][A-Za-z0-9_]*)\s*[=:\t]\s*(.+)$", line)
        if m and m.group(1) in COOKIE_TO_ENV:
            found[m.group(1)] = m.group(2).strip().strip('"').strip()
            pending_name = None
            continue

        # Case 2: a bare cookie name — its value is on a following line.
        # DevTools' "Copy" often leaves a trailing ':' or '=' on the name line
        # (e.g. "access_token:"), so normalise that away before matching.
        bare = line.rstrip(":= \t")
        if bare in COOKIE_TO_ENV:
            pending_name = bare
            continue

        # Case 3: this line is the value for the name we saw previously
        if pending_name:
            found[pending_name] = line.strip('"').strip()
            pending_name = None

    return found


def update_env(env_path: str, updates: dict[str, str]) -> tuple[int, int]:
    """
    Rewrite .env, replacing existing keys in place and appending new ones.
    Every other line (comments, Telegram settings, etc.) is preserved exactly.
    Returns (replaced_count, appended_count).
    """
    if os.path.exists(env_path):
        backup = f"{env_path}.backup-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(env_path, backup)
        print(f"  Backed up existing .env -> {os.path.basename(backup)}")
        with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    else:
        lines = []

    remaining = dict(updates)
    out: list[str] = []
    replaced = 0

    for line in lines:
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
            replaced += 1
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- Udemy session cookies (written by apply_cookies.py) ----")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")

    return replaced, len(remaining)


def main() -> None:
    ap = argparse.ArgumentParser(description="Write Udemy cookies from a text file into .env")
    ap.add_argument("cookie_file", help="Path to the cookies text file")
    ap.add_argument("--env", default=".env", help="Path to the .env file (default: .env)")
    ap.add_argument("--ua", default=None, help="Value of navigator.userAgent from the SAME browser")
    ap.add_argument("--user-id", default=None, help="Your numeric Udemy user id (ud_cache_user)")
    args = ap.parse_args()

    if not os.path.exists(args.cookie_file):
        print(f"ERROR: cookie file not found: {args.cookie_file}")
        sys.exit(1)

    print("=" * 66)
    print("  ScholarSync - apply_cookies.py")
    print("=" * 66)

    cookies = parse_cookie_file(args.cookie_file)

    if not cookies:
        print("\nERROR: no recognised cookies found in that file.")
        print("Expected names: " + ", ".join(COOKIE_TO_ENV))
        sys.exit(1)

    print(f"\nParsed {len(cookies)} cookie(s) from {args.cookie_file}:\n")
    updates: dict[str, str] = {}
    for name, value in cookies.items():
        env_key = COOKIE_TO_ENV[name]
        updates[env_key] = value
        print(f"  [OK] {name:<16} -> {env_key:<22} {mask(value)}")

    if args.ua:
        updates["UDEMY_USER_AGENT"] = args.ua.strip().strip("'\"")
        print(f"  [OK] {'user-agent':<16} -> {'UDEMY_USER_AGENT':<22} {updates['UDEMY_USER_AGENT'][:45]}...")

    if args.user_id:
        updates["UDEMY_USER_ID"] = args.user_id.strip()
        print(f"  [OK] {'ud_cache_user':<16} -> {'UDEMY_USER_ID':<22} {updates['UDEMY_USER_ID']}")

    missing = [c for c in REQUIRED if c not in cookies]
    if missing:
        print("\n  [!] WARNING - these REQUIRED cookies were not found:")
        for name in missing:
            print(f"      - {name}")
        print("      Without them Udemy's checkout page will treat the bot as")
        print("      logged out and enrollment cannot complete.")

    if "UDEMY_USER_AGENT" not in updates:
        print("\n  [!] No --ua supplied. cf_clearance is bound to the exact")
        print("      User-Agent that created it; if UDEMY_USER_AGENT in .env")
        print("      doesn't match, Cloudflare will reject the clearance cookie.")

    print()
    replaced, appended = update_env(args.env, updates)
    print(f"  Updated {args.env}: {replaced} key(s) replaced, {appended} added.")

    print()
    print("=" * 66)
    print("  Done. Now run:")
    print('    python test_enrollment.py "<your udemy course url with ?couponCode=>"')
    print("=" * 66)
    print()
    print("  SECURITY: these cookies are live credentials for your Udemy")
    print("  account. Delete the cookie text file once this has run, and")
    print("  never commit .env to git.")


if __name__ == "__main__":
    main()
