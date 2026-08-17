#!/bin/bash
# ScholarSync Admin Panel — guided setup.
#
# Run this ON THE VM, from the project root, after transferring admin_panel/
# via WinSCP:
#
#   cd /home/ubuntu/scholarsync
#   chmod +x admin_panel/deploy/setup_panel.sh
#   ./admin_panel/deploy/setup_panel.sh
#
# This script does the MECHANICAL steps (venv, pip install, cert, systemd,
# nginx, sudoers). It deliberately stops and tells you to do three things
# by hand, because they need your input or a value only you know:
#   1. set_password.py       — needs a password typed interactively
#   2. editing YOUR_HOME_IP  — only you know your own IP
#   3. the Oracle Cloud Security List rule — console-only, no file to copy
#
# Safe to re-run — every step either creates something that doesn't exist
# yet or overwrites its own previous output.

set -e
PROJECT_ROOT="$(pwd)"

echo "=================================================================="
echo "  ScholarSync Admin Panel setup"
echo "  Project root: $PROJECT_ROOT"
echo "=================================================================="

if [ ! -d "admin_panel" ]; then
  echo "ERROR: run this from the ScholarSync project root (admin_panel/ not found here)."
  exit 1
fi

echo
echo "[1/7] Creating virtual environment..."
python3 -m venv admin_panel/venv
source admin_panel/venv/bin/activate
pip install --upgrade pip -q
pip install -r admin_panel/requirements.txt -q
deactivate
echo "      Done."

echo
echo "[2/7] Admin account"
if [ -f "admin_panel/.env" ]; then
  echo "      admin_panel/.env already exists — skipping. To change the password later,"
  echo "      run: admin_panel/venv/bin/python admin_panel/set_password.py"
else
  echo "      No admin_panel/.env yet. Run this now (needs your input):"
  echo
  echo "          admin_panel/venv/bin/python admin_panel/set_password.py"
  echo
  echo "      Then re-run this script to continue."
  exit 0
fi

echo
echo "[3/7] Self-signed HTTPS certificate..."
if [ -f "/etc/ssl/scholarsync-panel/cert.pem" ]; then
  echo "      Certificate already exists — skipping."
else
  bash admin_panel/deploy/gen_self_signed_cert.sh
fi

echo
echo "[4/7] Installing systemd service..."
sudo cp admin_panel/deploy/scholarsync-panel.service /etc/systemd/system/scholarsync-panel.service
sudo systemctl daemon-reload
sudo systemctl enable scholarsync-panel
sudo systemctl restart scholarsync-panel
echo "      scholarsync-panel.service installed and started (listening on 127.0.0.1:8000 only)."

echo
echo "[5/7] Sudoers rule (lets the panel check/restart the bot service)..."
sudo visudo -cf admin_panel/deploy/sudoers_scholarsync_panel
sudo cp admin_panel/deploy/sudoers_scholarsync_panel /etc/sudoers.d/scholarsync-panel
sudo chmod 440 /etc/sudoers.d/scholarsync-panel
echo "      Installed."

echo
echo "[6/7] Log rotation policy (3-day forced rotation, 25MB safety trigger, 14-day retention)..."
sudo cp admin_panel/deploy/logrotate_scholarsync /etc/logrotate.d/scholarsync
sudo logrotate -d /etc/logrotate.d/scholarsync >/dev/null 2>&1 && echo "      Config OK." || echo "      WARNING: logrotate dry-run reported an issue — check manually with: sudo logrotate -d /etc/logrotate.d/scholarsync"
sudo cp admin_panel/deploy/scholarsync-logrotate.service /etc/systemd/system/scholarsync-logrotate.service
sudo cp admin_panel/deploy/scholarsync-logrotate.timer /etc/systemd/system/scholarsync-logrotate.timer
sudo systemctl daemon-reload
sudo systemctl enable --now scholarsync-logrotate.timer
echo "      Timer installed — forces a rotation every 3 days regardless of size."

echo
echo "[7/7] Done with the automated steps."
echo
echo "=================================================================="
echo "  MANUAL STEPS REMAINING before the panel is reachable:"
echo "=================================================================="
echo
echo "  A) Edit admin_panel/deploy/nginx_scholarsync_panel.conf and replace"
echo "     YOUR_HOME_IP with your real public IP (check it from your own"
echo "     machine at https://api.ipify.org, NOT from the VM). Then:"
echo
echo "         sudo cp admin_panel/deploy/nginx_scholarsync_panel.conf /etc/nginx/sites-available/scholarsync-panel"
echo "         sudo ln -sf /etc/nginx/sites-available/scholarsync-panel /etc/nginx/sites-enabled/"
echo "         sudo nginx -t && sudo systemctl reload nginx"
echo
echo "  B) In the Oracle Cloud console, add a Security List ingress rule for"
echo "     port 443 restricted to your IP/32 — this is the PRIMARY access"
echo "     control, more important than the nginx 'allow' line above."
echo "     (VCN -> Security Lists -> your subnet's list -> Add Ingress Rule)"
echo
echo "  Once both are done, open: https://$(curl -s ifconfig.me)/"
echo "  Your browser will warn about the self-signed cert once — that's expected."
echo "=================================================================="
