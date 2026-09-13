#!/usr/bin/env bash

set -euo pipefail

echo "======================================"
echo "Radio runtime setup"
echo "======================================"

if [ "${EUID}" -eq 0 ]; then
    echo "Run this as the pi user, not with sudo."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG="/boot/config.txt"
else
    echo "ERROR: Raspberry Pi boot config not found."
    exit 1
fi

echo
echo "1. Installing system packages..."

sudo apt update
sudo apt install -y \
    mpd \
    mpc \
    alsa-utils \
    i2c-tools \
    python3-pip \
    python3-venv \
    python3-yaml \
    python3-rpi.gpio

echo
echo "2. Configuring GPIO interfaces..."

# OLED requires I2C.
sudo raspi-config nonint do_i2c 0

# GPIO10/GPIO9 are used by SW1/SW2, so SPI must not own them.
sudo raspi-config nonint do_spi 1

# GPIO14/GPIO15 are used by the volume selector.
# Disable both serial console and UART hardware.
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 1

echo
echo "3. Configuring HiFiBerry MiniAmp..."

# Disable built-in Raspberry Pi audio if it is explicitly enabled.
if grep -qE '^[[:space:]]*dtparam=audio=on[[:space:]]*$' "$BOOT_CONFIG"; then
    sudo sed -i \
        's/^[[:space:]]*dtparam=audio=on[[:space:]]*$/dtparam=audio=off/' \
        "$BOOT_CONFIG"
fi

if ! grep -qE '^[[:space:]]*dtparam=audio=off[[:space:]]*$' "$BOOT_CONFIG"; then
    echo 'dtparam=audio=off' | sudo tee -a "$BOOT_CONFIG" >/dev/null
fi

# MiniAmp uses the HiFiBerry DAC overlay.
if ! grep -qE '^[[:space:]]*dtoverlay=hifiberry-dac([[:space:]]|$)' "$BOOT_CONFIG"; then
    echo 'dtoverlay=hifiberry-dac' | sudo tee -a "$BOOT_CONFIG" >/dev/null
fi

echo
echo "4. Installing ALSA configuration..."

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/config/asound.conf" \
    /etc/asound.conf

echo
echo "5. Configuring MPD..."

sudo install \
    -o root \
    -g root \
    -m 0644 \
    "$SCRIPT_DIR/config/mpd.conf" \
    /etc/mpd.conf

sudo mkdir -p /home/pi/audio
sudo chown pi:pi /home/pi/audio
sudo chmod 0755 /home/pi/audio

# Ensure MPD is allowed to open ALSA devices.
sudo usermod -aG audio mpd

sudo systemctl enable mpd

# Do not try to validate audio until after the HiFiBerry overlay has
# been loaded by a reboot.
sudo systemctl stop mpd || true

echo
echo "6. Creating radio Python virtual environment..."

if [ ! -d /opt/radio-venv ]; then
    sudo python3 -m venv \
        --system-site-packages \
        /opt/radio-venv
fi

sudo /opt/radio-venv/bin/python3 -m pip install --upgrade pip

sudo /opt/radio-venv/bin/python3 -m pip install \
    -r "$SCRIPT_DIR/requirements-pi.txt"

echo
echo "======================================"
echo "Runtime setup complete."
echo
echo "A reboot is required before testing"
echo "the HiFiBerry audio device."
echo "======================================"