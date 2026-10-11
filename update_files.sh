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

echo
echo "Installing station sync updater..."

sudo install \
    -o root \
    -g root \
    -m 0755 \
    "$SCRIPT_DIR/sync_stations.py" \
    /usr/local/bin/sync_stations.py

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/radio.service" \
    /etc/systemd/system/radio.service

echo
echo "Reloading systemd..."

echo
echo "Installing station sync service..."

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/radio-stations-sync.service" \
    /etc/systemd/system/radio-stations-sync.service



echo
echo "Installing station sync timer..."

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/radio-stations-sync.timer" \
    /etc/systemd/system/radio-stations-sync.timer


echo
echo "Installing Wi-Fi setup fallback..."

sudo install \
    -o root \
    -g root \
    -m 0755 \
    "$SCRIPT_DIR/radio-wifi-setup.sh" \
    /usr/local/bin/radio-wifi-setup.sh

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/radio-wifi-setup.service" \
    /etc/systemd/system/radio-wifi-setup.service

sudo systemctl daemon-reload
sudo systemctl enable radio-stations-sync.timer
sudo systemctl enable radio-wifi-setup.service

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