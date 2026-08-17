#!/bin/bash
# Generates a self-signed HTTPS certificate for the admin panel.
#
# A self-signed cert still fully encrypts the connection — it's exactly as
# secure in transit as a "real" cert. The only difference is your browser
# shows a one-time warning because no public authority vouches for it. Once
# you have a domain name pointed at this VM, swap this for a free Let's
# Encrypt cert via `sudo certbot --nginx` instead (no code changes needed).
set -e

sudo mkdir -p /etc/ssl/scholarsync-panel
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/ssl/scholarsync-panel/key.pem \
  -out /etc/ssl/scholarsync-panel/cert.pem \
  -subj "/CN=scholarsync-panel"
sudo chmod 600 /etc/ssl/scholarsync-panel/key.pem

echo
echo "Self-signed certificate created at /etc/ssl/scholarsync-panel/"
echo "Your browser will show a one-time trust warning the first time you visit — that's expected."
