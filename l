[1mdiff --git a/setup_runtime.sh b/setup_runtime.sh[m
[1mindex 5f55d78..ba8b0c7 100644[m
[1m--- a/setup_runtime.sh[m
[1m+++ b/setup_runtime.sh[m
[36m@@ -3,7 +3,7 @@[m
 set -euo pipefail[m
 [m
 echo "======================================"[m
[31m-echo "Radio runtime setup"[m
[32m+[m[32mecho "RadioFall2026 runtime setup"[m
 echo "======================================"[m
 [m
 if [ "${EUID}" -eq 0 ]; then[m
[36m@@ -39,21 +39,19 @@[m [msudo apt install -y \[m
 echo[m
 echo "2. Configuring GPIO interfaces..."[m
 [m
[31m-# OLED requires I2C.[m
[32m+[m[32m# OLED uses I2C.[m
 sudo raspi-config nonint do_i2c 0[m
 [m
[31m-# GPIO10/GPIO9 are used by SW1/SW2, so SPI must not own them.[m
[32m+[m[32m# GPIO10/GPIO9 are used by the maintained switches.[m
 sudo raspi-config nonint do_spi 1[m
 [m
 # GPIO14/GPIO15 are used by the volume selector.[m
[31m-# Disable both serial console and UART hardware.[m
 sudo raspi-config nonint do_serial_cons 1[m
 sudo raspi-config nonint do_serial_hw 1[m
 [m
 echo[m
 echo "3. Configuring HiFiBerry MiniAmp..."[m
 [m
[31m-# Disable built-in Raspberry Pi audio if it is explicitly enabled.[m
 if grep -qE '^[[:space:]]*dtparam=audio=on[[:space:]]*$' "$BOOT_CONFIG"; then[m
     sudo sed -i \[m
         's/^[[:space:]]*dtparam=audio=on[[:space:]]*$/dtparam=audio=off/' \[m
[36m@@ -64,7 +62,6 @@[m [mif ! grep -qE '^[[:space:]]*dtparam=audio=off[[:space:]]*$' "$BOOT_CONFIG"; then[m
     echo 'dtparam=audio=off' | sudo tee -a "$BOOT_CONFIG" >/dev/null[m
 fi[m
 [m
[31m-# MiniAmp uses the HiFiBerry DAC overlay.[m
 if ! grep -qE '^[[:space:]]*dtoverlay=hifiberry-dac([[:space:]]|$)' "$BOOT_CONFIG"; then[m
     echo 'dtoverlay=hifiberry-dac' | sudo tee -a "$BOOT_CONFIG" >/dev/null[m
 fi[m
[36m@@ -93,17 +90,11 @@[m [msudo mkdir -p /home/pi/audio[m
 sudo chown pi:pi /home/pi/audio[m
 sudo chmod 0755 /home/pi/audio[m
 [m
[31m-# Ensure MPD is allowed to open ALSA devices.[m
 sudo usermod -aG audio mpd[m
[31m-[m
[31m-sudo systemctl enable mpd[m
[31m-[m
[31m-# Do not try to validate audio until after the HiFiBerry overlay has[m
[31m-# been loaded by a reboot.[m
[31m-sudo systemctl stop mpd || true[m
[32m+[m[32msudo systemctl enable mpd.service[m
 [m
 echo[m
[31m-echo "6. Creating radio Python virtual environment..."[m
[32m+[m[32mecho "6. Creating radio Python environment..."[m
 [m
 if [ ! -d /opt/radio-venv ]; then[m
     sudo python3 -m venv \[m
[36m@@ -116,10 +107,58 @@[m [msudo /opt/radio-venv/bin/python3 -m pip install --upgrade pip[m
 sudo /opt/radio-venv/bin/python3 -m pip install \[m
     -r "$SCRIPT_DIR/requirements-pi.txt"[m
 [m
[32m+[m[32mecho[m
[32m+[m[32mecho "7. Installing radio files..."[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o root \[m
[32m+[m[32m    -g root \[m
[32m+[m[32m    -m 0755 \[m
[32m+[m[32m    "$SCRIPT_DIR/radio.py" \[m
[32m+[m[32m    /usr/local/bin/radio.py[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o pi \[m
[32m+[m[32m    -g pi \[m
[32m+[m[32m    -m 0644 \[m
[32m+[m[32m    "$SCRIPT_DIR/stations.yaml" \[m
[32m+[m[32m    /home/pi/stations.yaml[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "8. Installing radio service..."[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o root \[m
[32m+[m[32m    -g root \[m
[32m+[m[32m    -m 0644 \[m
[32m+[m[32m    "$SCRIPT_DIR/radio.service" \[m
[32m+[m[32m    /etc/systemd/system/radio.service[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "9. Configuring volatile journal..."[m
[32m+[m
[32m+[m[32msudo mkdir -p /etc/systemd/journald.conf.d[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o root \[m
[32m+[m[32m    -g root \[m
[32m+[m[32m    -m 0644 \[m
[32m+[m[32m    "$SCRIPT_DIR/config/radio-volatile.conf" \[m
[32m+[m[32m    /etc/systemd/journald.conf.d/radio-volatile.conf[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "10. Enabling services..."[m
[32m+[m
[32m+[m[32msudo systemctl daemon-reload[m
[32m+[m[32msudo systemctl enable mpd.service[m
[32m+[m[32msudo systemctl enable radio.service[m
[32m+[m
 echo[m
 echo "======================================"[m
 echo "Runtime setup complete."[m
 echo[m
[31m-echo "A reboot is required before testing"[m
[31m-echo "the HiFiBerry audio device."[m
[32m+[m[32mecho "Reboot before using the radio:"[m
[32m+[m[32mecho[m
[32m+[m[32mecho "    sudo reboot"[m
[32m+[m[32mecho[m
 echo "======================================"[m
\ No newline at end of file[m
[1mdiff --git a/update_files.sh b/update_files.sh[m
[1mindex b7c99c6..9ad1b2f 100755[m
[1m--- a/update_files.sh[m
[1m+++ b/update_files.sh[m
[36m@@ -1,19 +1,65 @@[m
[31m-#!/bin/bash[m
[31m-# update_files.sh — Deploy updated radio files and restart the service[m
[32m+[m[32m#!/usr/bin/env bash[m
[32m+[m
 set -euo pipefail[m
 [m
[31m-SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"[m
[32m+[m[32mSCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"[m
[32m+[m
[32m+[m[32mecho "======================================"[m
[32m+[m[32mecho "Deploying RadioFall2026"[m
[32m+[m[32mecho "======================================"[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Installing radio.py..."[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o root \[m
[32m+[m[32m    -g root \[m
[32m+[m[32m    -m 0755 \[m
[32m+[m[32m    "$SCRIPT_DIR/radio.py" \[m
[32m+[m[32m    /usr/local/bin/radio.py[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Installing stations.yaml..."[m
[32m+[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o pi \[m
[32m+[m[32m    -g pi \[m
[32m+[m[32m    -m 0644 \[m
[32m+[m[32m    "$SCRIPT_DIR/stations.yaml" \[m
[32m+[m[32m    /home/pi/stations.yaml[m
 [m
[31m-echo "→ Deploying radio.py..."[m
[31m-sudo cp "$SCRIPT_DIR/radio.py" /usr/local/bin/radio.py[m
[31m-sudo chmod +x /usr/local/bin/radio.py[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Installing radio.service..."[m
 [m
[31m-echo "→ Syncing stations.yaml..."[m
[31m-sudo cp "$SCRIPT_DIR/stations.yaml" /home/pi/stations.yaml[m
[31m-sudo chown pi:pi /home/pi/stations.yaml[m
[32m+[m[32msudo install \[m
[32m+[m[32m    -o root \[m
[32m+[m[32m    -g root \[m
[32m+[m[32m    -m 0644 \[m
[32m+[m[32m    "$SCRIPT_DIR/radio.service" \[m
[32m+[m[32m    /etc/systemd/system/radio.service[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Reloading systemd..."[m
 [m
[31m-echo "→ Restarting radio service..."[m
 sudo systemctl daemon-reload[m
[31m-sudo systemctl restart radio[m
 [m
[31m-echo "=== Files deployed ==="[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Restarting radio..."[m
[32m+[m
[32m+[m[32msudo systemctl restart radio.service[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "Checking service..."[m
[32m+[m
[32m+[m[32mif systemctl is-active --quiet radio.service; then[m
[32m+[m[32m    echo "Radio is running."[m
[32m+[m[32melse[m
[32m+[m[32m    echo "ERROR: radio.service did not start."[m
[32m+[m[32m    sudo systemctl status radio.service --no-pager[m
[32m+[m[32m    exit 1[m
[32m+[m[32mfi[m
[32m+[m
[32m+[m[32mecho[m
[32m+[m[32mecho "======================================"[m
[32m+[m[32mecho "Deployment complete."[m
[32m+[m[32mecho "======================================"[m
\ No newline at end of file[m
