#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================"
echo "Deploying RadioFall2026"
echo "======================================"

echo
echo "Installing radio.py..."

sudo install \
    -o root \
    -g root \
    -m 0755 \
    "$SCRIPT_DIR/radio.py" \
    /usr/local/bin/radio.py

echo
echo "Installing stations.yaml..."

sudo install \
    -o pi \
    -g pi \
    -m 0644 \
    "$SCRIPT_DIR/stations.yaml" \
    /home/pi/stations.yaml

echo
echo "Installing radio.service..."

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/radio.service" \
    /etc/systemd/system/radio.service

echo
echo "Reloading systemd..."

sudo systemctl daemon-reload

echo
echo "Restarting radio..."

sudo systemctl restart radio.service

echo
echo "Checking service..."

if systemctl is-active --quiet radio.service; then
    echo "Radio is running."
else
    echo "ERROR: radio.service did not start."
    sudo systemctl status radio.service --no-pager
    exit 1
fi

echo
echo "======================================"
echo "Deployment complete."
echo "======================================"