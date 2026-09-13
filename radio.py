#!/usr/bin/env python3
"""Raspberry Pi radio controller with bank/station selectors and OLED display.

Single script that handles:
- BCD rotary switch for bank selection (10 positions)
- BCD rotary switch for station selection (10 positions)
- BCD rotary switch used as a relative volume control (10 positions)
- Maintained play/pause switch
- Maintained prepare-for-power-loss switch with orderly shutdown
- 128x32 I2C OLED display
- MPD playback via mpc commands
- Stream watchdog (auto-restarts dead streams)
- Volume persistence across power loss
- Systemd watchdog integration

Bank, station, play/pause, and shutdown states are physical maintained controls and
are therefore read from hardware at startup rather than restored from disk.
"""

import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import board
import busio
import RPi.GPIO as GPIO
import yaml
from PIL import Image, ImageDraw, ImageFont

import adafruit_ssd1306


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

STATIONS_PATH = Path("/home/pi/stations.yaml")
AUDIO_ROOT = Path("/home/pi/audio")
AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac")
STATE_PATH = Path("/home/pi/state.json")
STATE_BACKUP_PATH = Path("/home/pi/state.backup.json")


# -----------------------------------------------------------------------------
# Hardware pin mappings
# -----------------------------------------------------------------------------

# IMPORTANT: these are BCM GPIO numbers. Comments show physical header pins.
# The FR01 switches were verified on hardware to decode correctly only with the
# bit significance reversed relative to the harness labels, so these mappings
# are intentionally reversed.

# Volume BCD: physical 7(V1), 8(V2), 10(V4), 11(V8)
VOLUME_PINS = {
    "bit0": 17,  # physical 11
    "bit1": 15,  # physical 10 (RXD)
    "bit2": 14,  # physical 8  (TXD)
    "bit3": 4,   # physical 7
}

# Bank BCD: physical 13(B1), 15(B2), 16(B4), 18(B8)
BANK_PINS = {
    "bit0": 24,  # physical 18
    "bit1": 23,  # physical 16
    "bit2": 22,  # physical 15
    "bit3": 27,  # physical 13
}

# Station BCD: physical 29(S1), 31(S2), 32(S4), 33(S8)
STATION_PINS = {
    "bit0": 13,  # physical 33
    "bit1": 12,  # physical 32
    "bit2": 6,   # physical 31
    "bit3": 5,   # physical 29
}

# Maintained SPST switches
PLAY_PAUSE_PIN = 10  # SW1, physical 19 (MOSI)
SHUTDOWN_PIN = 9     # SW2, physical 21 (MISO)

# Physical 22(GPIO25), 23(GPIO11/SCLK), 24(GPIO8/CE0), and 26(GPIO7/CE1)
# are terminated in the harness and intentionally not claimed by this program.

# OLED display: Adafruit 4440, SSD1306 128x32, I2C address 0x3C
OLED_I2C_ADDR = 0x3C
OLED_WIDTH = 128
OLED_HEIGHT = 32


# -----------------------------------------------------------------------------
# Volume and timing
# -----------------------------------------------------------------------------

VOLUME_MIN = 5
VOLUME_MAX = 100
DEFAULT_VOLUME = 25
VOLUME_STEP = 4

POLL_INTERVAL = 0.01
DEBOUNCE_TIME = 0.040
WATCHDOG_INTERVAL = 10.0
WATCHDOG_GRACE = 15.0
STATE_SAVE_INTERVAL = 5.0
CONFIG_CHECK_INTERVAL = 30.0
WATCHDOG_NOTIFY_INTERVAL = 10.0
DISPLAY_UPDATE_INTERVAL = 0.5


# -----------------------------------------------------------------------------
# Logging and process shutdown
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("radio")

_shutdown = False


def _handle_signal(signum, frame):
    """Handle SIGTERM/SIGINT for graceful application shutdown."""
    global _shutdown
    _shutdown = True
    log.info("Received signal %d, shutting down", signum)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# -----------------------------------------------------------------------------
# systemd watchdog
# -----------------------------------------------------------------------------


def _sd_notify(msg: bytes):
    """Send a notification message to systemd via NOTIFY_SOCKET."""
    try:
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr.startswith("@"):
            addr = "\0" + addr[1:]

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(msg)
        finally:
            sock.close()
    except Exception:
        # Watchdog notification failure must not crash radio playback.
        pass


def _notify_watchdog():
    _sd_notify(b"WATCHDOG=1")


def _notify_ready():
    _sd_notify(b"READY=1")


# -----------------------------------------------------------------------------
# Persistent state
# -----------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict):
    """Atomically write JSON and fsync both the file and parent directory."""
    dirpath = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dirpath), suffix=".tmp")

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, str(path))

        dfd = os.open(str(dirpath), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_state(volume):
    """Persist software volume. Physical control positions are not persisted."""
    state = {
        "volume": int(volume),
        "timestamp": int(time.time()),
    }

    try:
        _atomic_write_json(STATE_PATH, state)
        _atomic_write_json(STATE_BACKUP_PATH, state)
    except Exception as e:
        log.warning("Failed to save state: %s", e)


def _validate_state(data):
    """Return normalized persisted state, or None when invalid."""
    if not isinstance(data, dict):
        return None

    volume = data.get("volume")
    if not isinstance(volume, int):
        return None
    if not (VOLUME_MIN <= volume <= VOLUME_MAX):
        return None

    return {
        "volume": volume,
        "timestamp": data.get("timestamp"),
    }


def load_state():
    """Load saved volume, falling back to the backup state file."""
    for path in (STATE_PATH, STATE_BACKUP_PATH):
        try:
            if not path.exists():
                continue

            with open(path) as f:
                data = json.load(f)

            validated = _validate_state(data)
            if validated:
                log.info(
                    "Restored state from %s: volume=%d",
                    path.name,
                    validated["volume"],
                )
                return validated

            log.warning("Invalid state data in %s", path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not load %s: %s", path, e)

    return None


# -----------------------------------------------------------------------------
# GPIO/control helpers
# -----------------------------------------------------------------------------


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

    # Internal pull-ups make open contacts HIGH; selected contacts close to GND.
    # Invert exactly once.
    value = raw ^ 0xF

    if 0 <= value <= 9:
        return value
    return None


class DebouncedValue:
    """Accept a complete control value only after it remains stable."""

    def __init__(self, initial_value, settle_time=DEBOUNCE_TIME):
        self.stable = initial_value
        self.candidate = initial_value
        self.candidate_since = time.monotonic()
        self.settle_time = settle_time

    def update(self, value, now):
        """Return (old, new) when a new stable value is accepted, else None."""
        # Invalid BCD words are never accepted and restart the stability window.
        if value is None:
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
    """Return +1 for one CW step, -1 for one CCW step, or 0 for a jump."""
    if old_pos == 9 and new_pos == 0:
        return 1
    if old_pos == 0 and new_pos == 9:
        return -1
    if new_pos == old_pos + 1:
        return 1
    if new_pos == old_pos - 1:
        return -1
    return 0


def clamp(value, low, high):
    return max(low, min(high, value))


def read_stable_bcd_at_startup(pins, label, timeout=1.0):
    """Wait for a valid BCD word to remain stable for DEBOUNCE_TIME."""
    deadline = time.monotonic() + timeout
    candidate = None
    candidate_since = None

    while time.monotonic() < deadline:
        now = time.monotonic()
        value = read_bcd(pins)

        if value is None:
            candidate = None
            candidate_since = None
        elif value != candidate:
            candidate = value
            candidate_since = now
        elif candidate_since is not None and now - candidate_since >= DEBOUNCE_TIME:
            return value

        time.sleep(POLL_INTERVAL)

    log.warning("Could not get stable %s selector at startup; using 0", label)
    return 0


def read_stable_switch_at_startup(pin, label, timeout=1.0):
    """Wait for a maintained SPST state to remain stable for DEBOUNCE_TIME."""
    deadline = time.monotonic() + timeout
    candidate = None
    candidate_since = None

    while time.monotonic() < deadline:
        now = time.monotonic()
        value = GPIO.input(pin) == GPIO.LOW

        if value != candidate:
            candidate = value
            candidate_since = now
        elif candidate_since is not None and now - candidate_since >= DEBOUNCE_TIME:
            return value

        time.sleep(POLL_INTERVAL)

    log.warning("Could not get stable %s switch state at startup; treating as open", label)
    return False


# -----------------------------------------------------------------------------
# stations.yaml helpers
# -----------------------------------------------------------------------------


def load_stations():
    """Load stations.yaml and return the bank dictionary."""
    if not STATIONS_PATH.exists():
        log.error("stations.yaml not found at %s", STATIONS_PATH)
        return {}

    try:
        with open(STATIONS_PATH) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("Failed to load stations.yaml: %s", e)
        return {}

    banks = data.get("banks", {})
    if not isinstance(banks, dict):
        log.error("stations.yaml 'banks' key must be a mapping")
        return {}

    return banks


def get_station(banks, bank_id, station_id):
    """Resolve an absolute bank/station selection."""
    bank = banks.get(bank_id)
    if not isinstance(bank, dict):
        return None, None

    stations = bank.get("stations", {})
    if not isinstance(stations, dict):
        return bank, None

    station = stations.get(station_id)
    if not isinstance(station, dict):
        return bank, None

    return bank, station


def describe_selection(banks, bank_id, station_id):
    bank, station = get_station(banks, bank_id, station_id)

    if bank is None:
        return f"B{bank_id} S{station_id}: bank not configured"

    bank_name = bank.get("name", f"Bank {bank_id}")
    if station is None:
        return f"B{bank_id} S{station_id}: {bank_name} / not configured"

    station_name = station.get("name", "Unknown")
    return f"B{bank_id} S{station_id}: {bank_name} / {station_name}"


# -----------------------------------------------------------------------------
# MPD helpers
# -----------------------------------------------------------------------------


def mpc(*args):
    """Run an mpc command and return stdout. Errors are logged, not raised."""
    try:
        result = subprocess.run(
            ["mpc"] + list(args),
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            log.warning(
                "mpc %s failed (code %d): %s",
                " ".join(args),
                result.returncode,
                stderr,
            )

        return result.stdout.strip()
    except Exception as e:
        log.warning("mpc %s failed: %s", " ".join(args), e)
        return ""


def wait_for_mpd(retries=15, delay=2.0):
    """Block until MPD is reachable."""
    for i in range(retries):
        if _shutdown:
            return False

        try:
            result = subprocess.run(
                ["mpc", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            log.error("mpc command not found")
            return False
        except Exception as e:
            log.warning("Unable to query MPD: %s", e)
            result = None

        if result is not None and result.returncode == 0:
            log.info("MPD ready (attempt %d/%d)", i + 1, retries)
            return True

        log.warning("Waiting for MPD... (%d/%d)", i + 1, retries)
        time.sleep(delay)

    log.error("MPD not available after %d attempts", retries)
    return False


# -----------------------------------------------------------------------------
# OLED
# -----------------------------------------------------------------------------


def init_display(i2c):
    """Initialize the SSD1306 128x32 OLED display."""
    try:
        display = adafruit_ssd1306.SSD1306_I2C(
            OLED_WIDTH,
            OLED_HEIGHT,
            i2c,
            addr=OLED_I2C_ADDR,
        )
        display.fill(0)
        display.show()
        log.info("OLED display ready at 0x%02x", OLED_I2C_ADDR)
        return display
    except Exception as e:
        log.error("OLED init failed: %s", e)
        return None


_default_font = ImageFont.load_default()


def update_display(
    display,
    bank_id,
    station_id,
    station_name,
    volume,
    play_enabled,
    shutdown_requested=False,
):
    """Render current selection, station name, volume, and control state."""
    if display is None:
        return

    try:
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        font = _default_font

        if shutdown_requested:
            line1 = "POWER LOSS REQUEST"
            line2 = "Shutdown not armed"
            line3 = f"Vol: {volume}%"
        else:
            state_text = "PLAY" if play_enabled else "PAUSE"
            line1 = f"B{bank_id} S{station_id}  {state_text}"
            line2 = station_name[:21] if station_name else "---"
            line3 = f"Vol: {volume}%"

        draw.text((0, 0), line1[:21], font=font, fill=255)
        draw.text((0, 11), line2[:21], font=font, fill=255)
        draw.text((0, 22), line3[:21], font=font, fill=255)

        display.image(image)
        display.show()
    except Exception as e:
        log.warning("Display update failed: %s", e)


def show_powering_off(display):
    """Show a brief shutdown message while the system is still responsive."""
    if display is None:
        return

    try:
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), "POWERING OFF", font=_default_font, fill=255)
        draw.text((0, 11), "Please wait...", font=_default_font, fill=255)

        display.image(image)
        display.show()

        # Give the user a moment to see the shutdown message.
        time.sleep(0.75)
    except Exception as e:
        log.warning("OLED shutdown message failed: %s", e)


def power_down_display(display):
    """Blank the OLED and put it into its low-power state when supported."""
    if display is None:
        return

    try:
        display.fill(0)
        display.show()

        # CircuitPython SSD1306 supports poweroff(); keep this defensive
        # in case a future display implementation does not.
        poweroff_method = getattr(display, "poweroff", None)
        if callable(poweroff_method):
            poweroff_method()
    except Exception as e:
        log.warning("OLED shutdown failed: %s", e)


def prepare_for_power_loss(display, volume):
    """Safely prepare the radio and operating system for removal of power."""
    log.warning("Preparing radio for power loss")

    # Silence playback immediately.
    mpc("stop")

    # Preserve the user's real software volume before temporarily muting MPD.
    save_state(volume)

    # Leave the audio path muted while the machine shuts down.
    mpc("volume", "0")

    # Flush our persisted state and other pending filesystem writes.
    try:
        os.sync()
        log.info("Filesystem writes flushed")
    except Exception as e:
        log.warning("Filesystem sync failed: %s", e)

    # Tell the user what is happening before asking systemd to power off.
    # Keep the panel powered until the request succeeds so a failed request
    # can return to normal operation without needing to reinitialize the OLED.
    show_powering_off(display)

    # Flush once more after all shutdown preparation is complete.
    try:
        os.sync()
    except Exception:
        pass

    log.warning("Requesting orderly system poweroff")

    try:
        result = subprocess.run(
            ["systemctl", "--no-block", "poweroff"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        log.error("Unable to request system poweroff: %s", e)
        return False

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        log.error(
            "systemctl poweroff failed (code %d): %s",
            result.returncode,
            error,
        )
        return False

    # The orderly shutdown request was accepted; the display can now be blanked.
    power_down_display(display)
    log.warning("System poweroff successfully requested")
    return True


# -----------------------------------------------------------------------------
# Playback
# -----------------------------------------------------------------------------


def play_stream(url):
    log.info("Playing stream: %s", url)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", url)
    mpc("play")


def _resolve_path(raw: str) -> Path:
    """Resolve a station path under AUDIO_ROOT and reject traversal outside it."""
    raw = raw.strip()
    candidate = Path(raw) if raw.startswith("/") else AUDIO_ROOT / raw

    try:
        candidate.resolve().relative_to(AUDIO_ROOT.resolve())
    except ValueError:
        log.error("Rejected path outside audio root: %s", candidate)
        return AUDIO_ROOT / "__invalid_path__"

    return candidate


def _mpd_relpath(path: Path) -> str:
    """Convert an audio path to MPD's path relative to AUDIO_ROOT."""
    try:
        return str(path.resolve().relative_to(AUDIO_ROOT.resolve()))
    except ValueError:
        log.error("Path outside audio root: %s", path)
        return ""


def _seek_random():
    """Seek to a random position in the current track when duration is known."""
    output = mpc("status")

    for line in output.splitlines():
        if "/" not in line or ":" not in line or "%" not in line:
            continue

        for token in line.split():
            if "/" not in token or ":" not in token:
                continue

            try:
                _, total_str = token.split("/", 1)
                parts = [int(p) for p in total_str.split(":")]

                if len(parts) == 2:
                    total_sec = parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    total_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                else:
                    return

                if total_sec > 10:
                    target = random.randint(0, total_sec - 5)
                    mpc("seek", str(target))
            except (ValueError, IndexError):
                pass

            return


def _wait_for_playing(timeout=2.0):
    """Poll MPD until playback begins, or until timeout."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if "[playing]" in mpc("status"):
            return True
        time.sleep(0.1)

    log.warning("Timed out waiting for [playing] state")
    return False


def play_file(path_str):
    """Play one local file on repeat, starting at a random position."""
    resolved = _resolve_path(path_str)
    if not resolved.exists():
        log.error("File not found: %s", resolved)
        return

    rel = _mpd_relpath(resolved)
    if not rel:
        return

    log.info("Playing file (loop): %s", rel)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", rel)
    mpc("repeat", "on")
    mpc("play")

    if _wait_for_playing():
        _seek_random()


def play_file_once(path_str):
    """Play one local file from the beginning exactly once."""
    resolved = _resolve_path(path_str)
    if not resolved.exists():
        log.error("File not found: %s", resolved)
        return

    rel = _mpd_relpath(resolved)
    if not rel:
        return

    log.info("Playing file once: %s", rel)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", rel)
    mpc("play")


def play_dir(path_str):
    """Play a directory from a random track/position, then continue in order."""
    resolved = _resolve_path(path_str)
    if not resolved.is_dir():
        log.error("Directory not found: %s", resolved)
        return

    files = sorted(
        [f for f in resolved.rglob("*") if f.suffix.lower() in AUDIO_EXTS],
        key=lambda p: str(p).lower(),
    )

    if not files:
        log.error("No audio files in: %s", resolved)
        return

    log.info("Playing directory: %s (%d files)", resolved, len(files))
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")

    rel_paths = [_mpd_relpath(f) for f in files]
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        return

    try:
        subprocess.run(
            ["mpc", "add"] + rel_paths,
            text=True,
            timeout=30,
            capture_output=True,
        )
    except Exception as e:
        log.warning("mpc batch add failed: %s", e)
        return

    start = random.randint(1, len(rel_paths))
    mpc("play", str(start))

    if _wait_for_playing():
        _seek_random()


def play_station(station):
    """Play one resolved station dictionary. Return True when accepted."""
    if not isinstance(station, dict):
        return False

    name = station.get("name", "Unknown")
    stype = station.get("type", "").strip().lower()
    log.info("Playing %s [%s]", name, stype)

    if stype == "stream":
        url = station.get("url", "").strip()
        if not url:
            log.error("Stream station '%s' has no url", name)
            return False
        play_stream(url)
        return True

    if stype == "file":
        path = station.get("path", "").strip()
        if not path:
            log.error("File station '%s' has no path", name)
            return False
        play_file(path)
        return True

    if stype == "dir":
        path = station.get("path", "").strip()
        if not path:
            log.error("Dir station '%s' has no path", name)
            return False
        play_dir(path)
        return True

    if stype in ("file_once", "single_file"):
        path = (station.get("path") or station.get("file") or "").strip()
        if not path:
            log.error("File-once station '%s' has no path", name)
            return False
        play_file_once(path)
        return True

    log.error("Unknown station type '%s' for '%s'", stype, name)
    return False


def select_station(banks, bank_id, station_id, play_enabled):
    """Resolve an absolute selection and optionally start playback."""
    _bank, station = get_station(banks, bank_id, station_id)
    log.info("Selection: %s", describe_selection(banks, bank_id, station_id))

    if station is None:
        if play_enabled:
            mpc("stop")
        return None

    if play_enabled and not play_station(station):
        return None

    return station


def selected_station_name(banks, bank_id, station_id):
    _bank, station = get_station(banks, bank_id, station_id)
    if station is None:
        return "---"
    return station.get("name", "Unknown")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main():
    log.info("=" * 40)
    log.info("Radio controller starting")
    log.info("=" * 40)

    if not wait_for_mpd():
        sys.exit(1)

    # GPIO setup
    GPIO.setmode(GPIO.BCM)

    for pin in BANK_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    for pin in STATION_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    for pin in VOLUME_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(PLAY_PAUSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(SHUTDOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # OLED setup
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
    except Exception as e:
        log.error("I2C init failed: %s", e)
        GPIO.cleanup()
        sys.exit(1)

    display = init_display(i2c)

    # Config/state
    saved_state = load_state()
    banks = load_stations()
    stations_mtime = STATIONS_PATH.stat().st_mtime if STATIONS_PATH.exists() else 0

    if not banks:
        log.error("No banks loaded - check stations.yaml")
        GPIO.cleanup()
        sys.exit(1)

    log.info("Loaded %d configured banks", len(banks))

    # Initial physical positions. Physical controls are authoritative, and each
    # startup value must satisfy the same 40 ms stability requirement used later.
    cur_volume_pos = read_stable_bcd_at_startup(VOLUME_PINS, "volume")
    cur_bank_pos = read_stable_bcd_at_startup(BANK_PINS, "bank")
    cur_station_pos = read_stable_bcd_at_startup(STATION_PINS, "station")
    play_enabled = read_stable_switch_at_startup(PLAY_PAUSE_PIN, "play/pause")
    shutdown_requested = read_stable_switch_at_startup(SHUTDOWN_PIN, "shutdown")

    # Software volume persists; physical volume selector position is only the
    # reference used to infer future direction.
    volume = DEFAULT_VOLUME
    if saved_state is not None:
        volume = saved_state["volume"]

    bank_debounce = DebouncedValue(cur_bank_pos)
    station_debounce = DebouncedValue(cur_station_pos)
    volume_debounce = DebouncedValue(cur_volume_pos)
    play_debounce = DebouncedValue(play_enabled)
    shutdown_debounce = DebouncedValue(shutdown_requested)

    playing_bank = None
    playing_station = None

    # Apply initial software volume.
    mpc("volume", str(volume))

    log.info(
        "Initial controls: bank=%d station=%d volume=%d%% volume_pos=%d play=%s shutdown=%s",
        cur_bank_pos,
        cur_station_pos,
        volume,
        cur_volume_pos,
        play_enabled,
        shutdown_requested,
    )
    log.info("Initial selection: %s", describe_selection(banks, cur_bank_pos, cur_station_pos))

    if shutdown_requested:
        log.warning(
            "Shutdown switch is already in PREPARE FOR POWER LOSS position at startup"
        )

        if prepare_for_power_loss(display, volume):
            GPIO.cleanup()
            return

        log.error("Poweroff request failed; continuing radio operation")
        shutdown_requested = False
        mpc("volume", str(volume))

    if play_enabled:
        station = select_station(banks, cur_bank_pos, cur_station_pos, True)
        if station is not None:
            playing_bank = cur_bank_pos
            playing_station = cur_station_pos
    else:
        log.info("Play/pause switch is PAUSE at startup")
        mpc("stop")

    update_display(
        display,
        cur_bank_pos,
        cur_station_pos,
        selected_station_name(banks, cur_bank_pos, cur_station_pos),
        volume,
        play_enabled,
        shutdown_requested,
    )

    # Runtime tracking
    watchdog_last_check = 0.0
    watchdog_stop_since = 0.0
    last_state_save = 0.0
    state_dirty = True
    last_config_check = 0.0
    last_watchdog_notify = 0.0
    last_display_update = time.monotonic()
    display_dirty = False

    _notify_ready()

    log.info("Entering main loop")

    while not _shutdown:
        try:
            now = time.monotonic()

            # Reload stations.yaml when changed.
            if now - last_config_check >= CONFIG_CHECK_INTERVAL:
                last_config_check = now
                try:
                    mt = STATIONS_PATH.stat().st_mtime
                    if mt != stations_mtime:
                        new_banks = load_stations()
                        if new_banks:
                            banks = new_banks
                            stations_mtime = mt
                            log.info("Reloaded stations.yaml (%d banks)", len(banks))
                            mpc("update")
                            display_dirty = True
                        else:
                            log.warning(
                                "Ignoring stations.yaml reload due to invalid/empty bank data"
                            )
                except FileNotFoundError:
                    pass

            # Volume rotary: relative behavior from absolute 0..9 positions.
            raw_vol = read_bcd(VOLUME_PINS)
            volume_change = volume_debounce.update(raw_vol, now)

            if volume_change is not None:
                old_pos, new_pos = volume_change
                direction = volume_direction(old_pos, new_pos)
                cur_volume_pos = new_pos

                if direction != 0:
                    old_volume = volume
                    volume = clamp(
                        volume + direction * VOLUME_STEP,
                        VOLUME_MIN,
                        VOLUME_MAX,
                    )

                    if volume != old_volume:
                        mpc("volume", str(volume))
                        log.info(
                            "Volume selector %d -> %d; volume %d%% -> %d%%",
                            old_pos,
                            new_pos,
                            old_volume,
                            volume,
                        )
                        state_dirty = True
                        display_dirty = True
                    else:
                        log.info(
                            "Volume selector %d -> %d; already at limit %d%%",
                            old_pos,
                            new_pos,
                            volume,
                        )
                else:
                    log.warning(
                        "Volume selector jumped %d -> %d; reference updated, volume unchanged",
                        old_pos,
                        new_pos,
                    )

            # Bank selector: absolute 0..9.
            raw_bank = read_bcd(BANK_PINS)
            bank_change = bank_debounce.update(raw_bank, now)

            if bank_change is not None:
                old_bank, new_bank = bank_change
                cur_bank_pos = new_bank
                log.info("Bank selector: %d -> %d", old_bank, new_bank)

                if play_enabled:
                    station = select_station(
                        banks,
                        cur_bank_pos,
                        cur_station_pos,
                        True,
                    )
                    if station is not None:
                        playing_bank = cur_bank_pos
                        playing_station = cur_station_pos
                        watchdog_stop_since = 0.0
                    else:
                        playing_bank = None
                        playing_station = None

                display_dirty = True

            # Station selector: absolute 0..9.
            raw_station = read_bcd(STATION_PINS)
            station_change = station_debounce.update(raw_station, now)

            if station_change is not None:
                old_station, new_station = station_change
                cur_station_pos = new_station
                log.info("Station selector: %d -> %d", old_station, new_station)

                if play_enabled:
                    station = select_station(
                        banks,
                        cur_bank_pos,
                        cur_station_pos,
                        True,
                    )
                    if station is not None:
                        playing_bank = cur_bank_pos
                        playing_station = cur_station_pos
                        watchdog_stop_since = 0.0
                    else:
                        playing_bank = None
                        playing_station = None

                display_dirty = True

            # Maintained play/pause switch.
            raw_play = GPIO.input(PLAY_PAUSE_PIN) == GPIO.LOW
            play_change = play_debounce.update(raw_play, now)

            if play_change is not None:
                _old_play, new_play = play_change
                play_enabled = new_play

                if play_enabled:
                    log.info("Play/pause switch -> PLAY")

                    # If the same source is still loaded and merely paused, resume it.
                    # Otherwise start whatever the physical selectors currently choose.
                    if (
                        playing_bank == cur_bank_pos
                        and playing_station == cur_station_pos
                        and "[paused]" in mpc("status")
                    ):
                        mpc("play")
                    else:
                        station = select_station(
                            banks,
                            cur_bank_pos,
                            cur_station_pos,
                            True,
                        )
                        if station is not None:
                            playing_bank = cur_bank_pos
                            playing_station = cur_station_pos
                        else:
                            playing_bank = None
                            playing_station = None

                    watchdog_stop_since = 0.0
                else:
                    log.info("Play/pause switch -> PAUSE")
                    mpc("pause")

                display_dirty = True

            # Prepare-for-power-loss switch: perform an orderly system shutdown.
            raw_shutdown = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW
            shutdown_change = shutdown_debounce.update(raw_shutdown, now)

            if shutdown_change is not None:
                _old_shutdown, new_shutdown = shutdown_change
                shutdown_requested = new_shutdown

                if shutdown_requested:
                    log.warning("Shutdown switch -> PREPARE FOR POWER LOSS")

                    if prepare_for_power_loss(display, volume):
                        # Exit the controller cleanly after the poweroff request.
                        break

                    log.error("Poweroff request failed; radio will continue running")
                    shutdown_requested = False
                    mpc("volume", str(volume))

                    if play_enabled:
                        station = select_station(
                            banks,
                            cur_bank_pos,
                            cur_station_pos,
                            True,
                        )
                        if station is not None:
                            playing_bank = cur_bank_pos
                            playing_station = cur_station_pos
                            watchdog_stop_since = 0.0
                        else:
                            playing_bank = None
                            playing_station = None

                    display_dirty = True

                else:
                    log.info("Shutdown switch -> RUN")
                    display_dirty = True


            # Stream watchdog.
            if play_enabled and now - watchdog_last_check >= WATCHDOG_INTERVAL:
                watchdog_last_check = now

                if playing_bank is not None and playing_station is not None:
                    _bank, stn = get_station(banks, playing_bank, playing_station)

                    if stn is not None and stn.get("type", "").strip().lower() == "stream":
                        status = mpc("status")

                        if "[playing]" not in status and "[paused]" not in status:
                            if watchdog_stop_since == 0.0:
                                watchdog_stop_since = now
                                log.warning(
                                    "Stream appears stopped, waiting %.0fs...",
                                    WATCHDOG_GRACE,
                                )
                            elif now - watchdog_stop_since >= WATCHDOG_GRACE:
                                log.info(
                                    "Watchdog: restarting B%d S%d",
                                    playing_bank,
                                    playing_station,
                                )
                                play_station(stn)
                                watchdog_stop_since = 0.0
                        else:
                            watchdog_stop_since = 0.0

            # OLED refresh.
            if display_dirty and now - last_display_update >= DISPLAY_UPDATE_INTERVAL:
                update_display(
                    display,
                    cur_bank_pos,
                    cur_station_pos,
                    selected_station_name(banks, cur_bank_pos, cur_station_pos),
                    volume,
                    play_enabled,
                    shutdown_requested,
                )
                last_display_update = now
                display_dirty = False

            # Persist only software volume.
            if state_dirty and now - last_state_save >= STATE_SAVE_INTERVAL:
                save_state(volume)
                last_state_save = now
                state_dirty = False

            # systemd watchdog keepalive.
            if now - last_watchdog_notify >= WATCHDOG_NOTIFY_INTERVAL:
                _notify_watchdog()
                last_watchdog_notify = now

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
            time.sleep(1.0)

    # Application shutdown (SIGTERM/SIGINT), not the physical power-loss switch.
    log.info("Shutting down radio process gracefully")
    save_state(volume)

    if display:
        try:
            display.fill(0)
            display.show()
        except Exception:
            pass

    GPIO.cleanup()


if __name__ == "__main__":
    main()
