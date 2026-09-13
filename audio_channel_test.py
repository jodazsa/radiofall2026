#!/usr/bin/env python3

import math
import struct
import subprocess
import wave
from pathlib import Path


OUTPUT = Path("/tmp/radio-channel-test.wav")

SAMPLE_RATE = 44100
AMPLITUDE = 0.08

TONE_SECONDS = 2.0
GAP_SECONDS = 0.5


def sample_value(freq, sample_number):
    angle = 2.0 * math.pi * freq * sample_number / SAMPLE_RATE
    return int(32767 * AMPLITUDE * math.sin(angle))


def write_test_file():
    tone_frames = int(TONE_SECONDS * SAMPLE_RATE)
    gap_frames = int(GAP_SECONDS * SAMPLE_RATE)

    with wave.open(str(OUTPUT), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)

        # LEFT-source-only tone.
        for i in range(tone_frames):
            left = sample_value(440.0, i)
            right = 0
            wav.writeframesraw(struct.pack("<hh", left, right))

        # Silence.
        for _ in range(gap_frames):
            wav.writeframesraw(struct.pack("<hh", 0, 0))

        # RIGHT-source-only tone.
        for i in range(tone_frames):
            left = 0
            right = sample_value(660.0, i)
            wav.writeframesraw(struct.pack("<hh", left, right))


def main():
    print("Creating low-volume stereo channel test...")
    write_test_file()

    print()
    print("You should hear:")
    print("  1. A lower tone sourced from LEFT")
    print("  2. A short pause")
    print("  3. A higher tone sourced from RIGHT")
    print()
    print("Both tones should come from the single LEFT speaker.")
    print()

    subprocess.run(
        ["aplay", "-D", "default", str(OUTPUT)],
        check=True,
    )

    print()
    print("Audio channel test finished.")


if __name__ == "__main__":
    main()