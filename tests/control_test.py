#!/usr/bin/env python3
"""Standalone GPIO test for the Fall 2026 radio control panel.

This test does not use MPD, the OLED, or systemd. It verifies:
- three 10-position BCD rotary switches
- 40 ms whole-value debounce
- relative volume direction, including 9<->0 wraparound
- two maintained SPST switches
"""

import time
import RPi.GPIO as GPIO

# ── Hardware pin mappings (BCM numbering) ──────────────────
# The bit order below was verified on the actual switches so physical
# positions decode as 0,1,2,3,4,5,6,7,8,9.

VOLUME_PINS = {
    "bit0": 7,   # physical 26
    "bit1": 12,  # physical 32
    "bit2": 13,  # physical 33
    "bit3": 6,   # physical 31
}

BANK_PINS = {
    "bit0": 5,   # physical 29
    "bit1": 11,  # physical 23
    "bit2": 9,   # physical 21
    "bit3": 10,  # physical 19
}

STATION_PINS = {
    "bit0": 22,  # physical 15
    "bit1": 27,  # physical 13
    "bit2": 17,  # physical 11
    "bit3": 4,   # physical 7
}

PLAY_STOP_PIN = 8  # physical 24
SHUTDOWN_PIN = 25   # physical 22

DEBOUNCE_TIME = 0.040
POLL_INTERVAL = 0.01


def read_bcd(pins):
    """Read an active-low 4-bit BCD selector and return 0..9 or None."""
    raw = 0

    if GPIO.input(pins["bit0"]):
        raw |= 1
    if GPIO.input(pins["bit1"]):
        raw |= 2
    if GPIO.input(pins["bit2"]):
        raw |= 4
    if GPIO.input(pins["bit3"]):
        raw |= 8

    # Pull-ups make open contacts HIGH and closed-to-ground contacts LOW.
    value = raw ^ 0xF

    if 0 <= value <= 9:
        return value

    return None


class DebouncedValue:
    """Accept a complete control value only after it is stable for 40 ms."""

    def __init__(self, initial_value, settle_time=DEBOUNCE_TIME):
        self.stable = initial_value
        self.candidate = initial_value
        self.candidate_since = time.monotonic()
        self.settle_time = settle_time

    def update(self, value, now):
        """Return (old, new) when a new stable value is accepted, else None."""
        if value is None:
            # Invalid BCD words are ignored and reset the candidate timer.
            self.candidate = self.stable
            self.candidate_since = now
            return None

        if value != self.candidate:
            self.candidate = value
            self.candidate_since = now
            return None

        if value != self.stable and now - self.candidate_since >= self.settle_time:
            old_value = self.stable
            self.stable = value
            return old_value, value

        return None


def volume_direction(old_pos, new_pos):
    """Return +1 for one CW step, -1 for one CCW step, 0 for a jump."""
    if old_pos == 9 and new_pos == 0:
        return 1
    if old_pos == 0 and new_pos == 9:
        return -1
    if new_pos == old_pos + 1:
        return 1
    if new_pos == old_pos - 1:
        return -1
    return 0


def setup_gpio():
    GPIO.setmode(GPIO.BCM)

    for pin in BANK_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    for pin in STATION_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    for pin in VOLUME_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(PLAY_STOP_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(SHUTDOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def wait_for_valid_bcd(label, pins):
    """Wait until a rotary presents a valid BCD word before starting the test."""
    while True:
        value = read_bcd(pins)
        if value is not None:
            return value
        print(f"Waiting for valid {label} position...")
        time.sleep(0.25)


def main():
    setup_gpio()

    try:
        bank = wait_for_valid_bcd("bank", BANK_PINS)
        station = wait_for_valid_bcd("station", STATION_PINS)
        volume = wait_for_valid_bcd("volume", VOLUME_PINS)
        play = GPIO.input(PLAY_STOP_PIN) == GPIO.LOW
        shutdown = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW

        bank_debounce = DebouncedValue(bank)
        station_debounce = DebouncedValue(station)
        volume_debounce = DebouncedValue(volume)
        play_debounce = DebouncedValue(play)
        shutdown_debounce = DebouncedValue(shutdown)

        print("Control test running. Press Ctrl+C to stop.")
        print(
            "INITIAL "
            f"bank={bank} station={station} volume={volume} "
            f"play_stop={'CLOSED' if play else 'OPEN'} "
            f"shutdown={'CLOSED' if shutdown else 'OPEN'}"
        )

        while True:
            now = time.monotonic()

            change = bank_debounce.update(read_bcd(BANK_PINS), now)
            if change is not None:
                old, new = change
                print(f"BANK     {old} -> {new}")

            change = station_debounce.update(read_bcd(STATION_PINS), now)
            if change is not None:
                old, new = change
                print(f"STATION  {old} -> {new}")

            change = volume_debounce.update(read_bcd(VOLUME_PINS), now)
            if change is not None:
                old, new = change
                direction = volume_direction(old, new)
                if direction > 0:
                    action = "UP"
                elif direction < 0:
                    action = "DOWN"
                else:
                    action = "JUMP - REFERENCE ONLY"
                print(f"VOLUME   {old} -> {new}  {action}")

            raw_play = GPIO.input(PLAY_STOP_PIN) == GPIO.LOW
            change = play_debounce.update(raw_play, now)
            if change is not None:
                _old, new = change
                print(f"PLAY     {'CLOSED / PLAY' if new else 'OPEN / STOP'}")

            raw_shutdown = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW
            change = shutdown_debounce.update(raw_shutdown, now)
            if change is not None:
                _old, new = change
                print(
                    "SHUTDOWN "
                    + ("CLOSED / PREPARE FOR POWER LOSS" if new else "OPEN / RUN")
                )

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping test.")

    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
