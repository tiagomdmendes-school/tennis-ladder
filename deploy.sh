#!/usr/bin/env bash
# Pull the latest code and restart the ladder. Run this on the server:
#
#     ./deploy.sh
#
# Safe to run any time. If nothing changed it just restarts.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> pulling"
git pull --ff-only

echo "==> restarting"
sudo systemctl restart ladder
sleep 1

if systemctl is-active --quiet ladder; then
    echo "==> ladder is running"
    systemctl status ladder --no-pager | head -4
else
    echo "!! ladder did NOT start. Last 30 log lines:" >&2
    journalctl -u ladder -n 30 --no-pager >&2
    exit 1
fi
