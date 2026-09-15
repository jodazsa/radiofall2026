# RadioFall2026

A Raspberry Pi radio with real knobs, a little OLED, internet radio, and offline audio.

- Bank knob picks the category
- Station knob picks the station
- Volume knob moves volume up/down
- Play/Pause switch does what you'd expect
- Rear switch puts the radio into a safe "OK to unplug" state
- OLED shows what the radio is doing

The radio runs on a Raspberry Pi Zero 2 W with a HiFiBerry MiniAmp and an SSD1306 128x32 OLED.

## Main Files

```text
radio.py
    Main radio program.

stations.yaml
    All station definitions.

radio.service
    systemd service that runs radio.py.

requirements-pi.txt
    Python packages needed by the radio.

setup_runtime.sh
    Sets up the Pi runtime, audio, I2C, MPD, and Python environment.

update_files.sh
    Copies radio.py and stations.yaml into their live locations
    and restarts the radio.

config/
    asound.conf
    mpd.conf
    radio-volatile.conf

tests/
    Hardware test scripts.