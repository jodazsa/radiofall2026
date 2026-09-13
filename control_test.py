#!/usr/bin/env python3

import time
import RPi.GPIO as GPIO

# BCM GPIO assignments

VOLUME_PINS = {
    "bit0": 17,   # physical pin 11
    "bit1": 15,   # physical pin 10
    "bit2": 14,   # physical pin 8
    "bit3": 4,    # physical pin 7
}

BANK_PINS = {
    "bit0": 24,   # physical pin 18
    "bit1": 23,   # physical pin 16
    "bit2": 22,   # physical pin 15
    "bit3": 27,   # physical pin 13
}

STATION_PINS = {
    "bit0": 13,   # physical pin 33
    "bit1": 12,   # physical pin 32
    "bit2": 6,    # physical pin 31
    "bit3": 5,    # physical pin 29
}

PLAY_PAUSE_PIN = 10
SHUTDOWN_PIN = 9

def read_bcd(pins):
    raw = 0

    if GPIO.input(pins["bit0"]):
        raw |= 1
    if GPIO.input(pins["bit1"]):
        raw |= 2
    if GPIO.input(pins["bit2"]):
        raw |= 4
    if GPIO.input(pins["bit3"]):
        raw |= 8

    # Inputs use pull-ups and switches close to ground,
    # so invert all four bits exactly once.
    value = raw ^ 0xF

    if 0 <= value <= 9:
        return value

    return None


def setup_gpio():
    GPIO.setmode(GPIO.BCM)

    for pin in BANK_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    for pin in STATION_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    for pin in VOLUME_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(PLAY_PAUSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(SHUTDOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def main():
    setup_gpio()

    print("Control test running.")
    print("Press Ctrl+C to stop.")
    print()

    try:
        while True:
            bank = read_bcd(BANK_PINS)
            station = read_bcd(STATION_PINS)
            volume = read_bcd(VOLUME_PINS)

            play_pause_closed = GPIO.input(PLAY_PAUSE_PIN) == GPIO.LOW
            shutdown_closed = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW

            print(
                f"bank={bank}  "
                f"station={station}  "
                f"volume={volume}  "
                f"play_pause={'CLOSED' if play_pause_closed else 'OPEN'}  "
                f"shutdown={'CLOSED' if shutdown_closed else 'OPEN'}"
            )

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopping test.")

    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()