# RadioFall2026

A Raspberry Pi radio with real knobs, a little OLED, internet radio, podcasts, and offline audio.

- Bank knob picks the category
- Station knob picks the station
- Volume knob moves volume up/down
- Play/Stop switch starts or stops the selected source
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


## Podcast Playback

Podcast stations use `type: podcast` with an RSS feed URL.

When a podcast station is selected, the radio:

- Fetches the RSS feed and uses the first playable item in feed order.
- Skips RSS items that do not contain a valid HTTP or HTTPS audio enclosure.
- Starts the selected episode from the beginning.
- Does not save or resume playback position.
- Does not repeat the episode.
- Does not download or cache episodes.
- Stops naturally when the episode ends.
- Fetches the feed again whenever the podcast station is selected again, so the current first playable episode is used.

Podcast playback is intentionally separate from the internet-stream watchdog. A completed podcast therefore remains stopped rather than being restarted automatically.