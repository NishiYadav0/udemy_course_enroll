"""
admin_panel/service_control.py
-------------------------------
Thin wrappers around `sudo systemctl ...` for the ScholarSync bot service.

Every call here uses `sudo -n` (non-interactive). If the sudoers rule from
deploy/sudoers_scholarsync_panel isn't installed on the VM, these calls fail
closed (return an error dict) rather than hanging on a password prompt.

Deliberately narrow: this module can only run the exact small set of
commands whitelisted in the sudoers file. It cannot run arbitrary shell.
"""

import re
import subprocess

SYSTEMCTL = "/usr/bin/systemctl"  # confirm with `which systemctl` on your VM


def _run(args: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except FileNotFoundError:
        return False, "systemctl not found on this host"
    except subprocess.TimeoutExpired:
        return False, "command timed out"
    except Exception as exc:  # never let a status check crash the dashboard
        return False, f"unexpected error: {exc}"


def is_active(service: str) -> str:
    """Returns 'active', 'inactive', 'failed', 'unknown', or 'no-permission'."""
    ok, output = _run(["sudo", "-n", SYSTEMCTL, "is-active", service])
    output = output.strip()
    if output in ("active", "inactive", "failed", "activating", "deactivating"):
        return output
    if "a password is required" in output.lower() or "not allowed" in output.lower():
        return "no-permission"
    return "unknown"


def status_summary(service: str) -> dict:
    """
    Returns a small dict of the fields the dashboard cares about. Uses one
    whitelisted `systemctl show` call (no --property filter) rather than
    parsing free-form `systemctl status` text, which is more fragile to
    sudoers matching.

    NOTE: earlier this used `--property=A,B,C`. Commas are a list separator
    in sudoers' own grammar, so an unescaped comma inside the allowed
    command's arguments breaks `visudo -c` with a parse error. Rather than
    escaping every comma (fragile — breaks again if this property list ever
    changes), we whitelist the plain `systemctl show <service>` command with
    no arguments and just parse the (larger) output for the fields we want.
    """
    ok, output = _run(["sudo", "-n", SYSTEMCTL, "show", service])
    if not ok:
        return {"ok": False, "error": output or "could not query systemctl"}

    fields = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v

    return {
        "ok": True,
        "active_state": fields.get("ActiveState", "unknown"),
        "sub_state": fields.get("SubState", "unknown"),
        "since": fields.get("ActiveEnterTimestamp", "unknown"),
        "main_pid": fields.get("MainPID", "0"),
        "restarts": fields.get("NRestarts", "0"),
    }


def restart(service: str) -> tuple[bool, str]:
    return _run(["sudo", "-n", SYSTEMCTL, "restart", service], timeout=20.0)


def enable_and_start(service: str) -> tuple[bool, str]:
    """First-ever launch only (setup wizard's final step) — 'restart' above
    assumes the service is already enabled and just needs a bounce; a brand
    new deploy needs it enabled (survive reboot) AND started for the first
    time. Requires the two extra sudoers lines added alongside this."""
    ok_enable, out_enable = _run(["sudo", "-n", SYSTEMCTL, "enable", service], timeout=10.0)
    ok_start, out_start = _run(["sudo", "-n", SYSTEMCTL, "start", service], timeout=20.0)
    return (ok_enable and ok_start), (out_enable + "\n" + out_start).strip()


def process_memory_mb(pid: str) -> float | None:
    """RSS memory of the bot process in MB, or None if it can't be read."""
    if not pid or pid == "0":
        return None
    ok, output = _run(["ps", "-o", "rss=", "-p", pid], timeout=5.0)
    if not ok:
        return None
    m = re.search(r"\d+", output)
    return round(int(m.group()) / 1024, 1) if m else None


def system_memory() -> dict | None:
    """Total / used / free RAM and swap (all in MB), from `free -m`."""
    ok, output = _run(["free", "-m"], timeout=5.0)
    if not ok:
        return None
    result = {}
    for line in output.splitlines():
        parts = line.split()
        if line.startswith("Mem:") and len(parts) >= 4:
            result.update({"total_mb": int(parts[1]), "used_mb": int(parts[2]), "free_mb": int(parts[3])})
        elif line.startswith("Swap:") and len(parts) >= 3:
            result.update({
                "swap_total_mb": int(parts[1]),
                "swap_used_mb": int(parts[2]),
                "swap_free_mb": int(parts[3]) if len(parts) > 3 else int(parts[1]) - int(parts[2]),
            })
    return result or None
