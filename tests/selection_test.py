#!/usr/bin/env python3

import time

import RPi.GPIO as GPIO
import yaml


DEBOUNCE_TIME = 0.040
POLL_INTERVAL = 0.01

BANK_PINS = {
    "bit0": 24,
    "bit1": 23,
    "bit2": 22,
    "bit3": 27,
}

STATION_PINS = {
    "bit0": 13,
    "bit1": 12,
    "bit2": 6,
    "bit3": 5,
}


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

    value = raw ^ 0xF

    if 0 <= value <= 9:
        return value

    return None


class DebouncedValue:
    def __init__(self, initial_value, settle_time=DEBOUNCE_TIME):
        self.stable = initial_value
        self.candidate = initial_value
        self.candidate_since = time.monotonic()
        self.settle_time = settle_time

    def update(self, value, now):
        if value is None:
            self.candidate = self.stable
            self.candidate_since = now
            return None

        if value != self.candidate:
            self.candidate = value
            self.candidate_since = now
            return None

        if (
            value != self.stable
            and now - self.candidate_since >= self.settle_time
        ):
            old = self.stable
            self.stable = value
            return old, value

        return None


def describe(banks, bank_id, station_id):
    bank = banks.get(bank_id)

    if not isinstance(bank, dict):
        return f"B{bank_id} S{station_id}: BANK NOT CONFIGURED"

    bank_name = bank.get("name", f"Bank {bank_id}")
    station = bank.get("stations", {}).get(station_id)

    if not isinstance(station, dict):
        return (
            f"B{bank_id} S{station_id}: "
            f"{bank_name} / STATION NOT CONFIGURED"
        )

    station_name = station.get("name", "Unknown")

    return (
        f"B{bank_id} S{station_id}: "
        f"{bank_name} / {station_name}"
    )


def main():
    with open("stations.yaml") as f:
        banks = yaml.safe_load(f).get("banks", {})

    GPIO.setmode(GPIO.BCM)

    for pin in BANK_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    for pin in STATION_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    bank = read_bcd(BANK_PINS)
    station = read_bcd(STATION_PINS)

    if bank is None:
        bank = 0

    if station is None:
        station = 0

    bank_debounce = DebouncedValue(bank)
    station_debounce = DebouncedValue(station)

    print("Bank/station selection test")
    print("Press Ctrl+C to stop.")
    print()
    print(describe(banks, bank, station))

    try:
        while True:
            now = time.monotonic()

            bank_change = bank_debounce.update(
                read_bcd(BANK_PINS),
                now,
            )

            if bank_change is not None:
                _, bank = bank_change
                print(describe(banks, bank, station))

            station_change = station_debounce.update(
                read_bcd(STATION_PINS),
                now,
            )

            if station_change is not None:
                _, station = station_change
                print(describe(banks, bank, station))

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping.")

    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()