#!/usr/bin/env python3
"""Raspberry Pi radio controller with bank/station selectors and OLED display.

Single script that handles:
- BCD rotary switch for bank selection (10 positions)
- BCD rotary switch for station selection (10 positions)
- BCD rotary switch used as a relative volume control (10 positions)
- Maintained play/stop switch
- Maintained power-loss standby switch with safe resume
- 128x32 I2C OLED display
- MPD playback via mpc commands
- Stream watchdog (auto-restarts dead streams)
- Volume persistence across power loss
- Systemd watchdog integration

Bank, station, play/stop, and shutdown states are physical maintained controls and
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
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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
# Each BCD rotary is listed in 1, 2, 4, 8 bit order.

# Volume BCD: physical 26(V1), 32(V2), 33(V4), 31(V8)
VOLUME_PINS = {
    "bit0": 7,   # physical 26
    "bit1": 12,  # physical 32
    "bit2": 13,  # physical 33
    "bit3": 6,   # physical 31
}

# Bank BCD: physical 29(B1), 23(B2), 21(B4), 19(B8)
BANK_PINS = {
    "bit0": 5,   # physical 29
    "bit1": 11,  # physical 23 (SPI0 SCLK)
    "bit2": 9,   # physical 21 (SPI0 MISO)
    "bit3": 10,  # physical 19 (SPI0 MOSI)
}

# Station BCD: physical 15(S1), 13(S2), 11(S4), 7(S8)
STATION_PINS = {
    "bit0": 22,  # physical 15
    "bit1": 27,  # physical 13
    "bit2": 17,  # physical 11
    "bit3": 4,   # physical 7
}

# Maintained SPST switches
PLAY_STOP_PIN = 8   # physical 24 (SPI0 CE0)
SHUTDOWN_PIN = 25   # physical 22


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
PODCAST_FETCH_TIMEOUT = 8
PODCAST_MAX_BYTES = 2 * 1024 * 1024
PODCAST_READ_CHUNK = 64 * 1024

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
    bank_name,
    station_name,
    volume,
    play_enabled,
    shutdown_requested=False,
):
    """Render the OLED for the current radio state."""
    if display is None:
        return

    try:
        # Rear power-loss standby always gets priority.
        if shutdown_requested:
            image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
            draw = ImageDraw.Draw(image)

            draw.text((0, 0), "OK to unplug", font=_default_font, fill=255)
            draw.text((0, 11), "Flip rear switch", font=_default_font, fill=255)
            draw.text((0, 22), "to ON to resume", font=_default_font, fill=255)

            display.image(image)
            display.show()
            return

        # PLAY/STOP switch in STOP position: make the radio look off.
        if not play_enabled:
            display.fill(0)
            display.show()
            return

        # Normal PLAY display.
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)

        line1 = f"Bank: {bank_name}"
        line2 = station_name[:21] if station_name else "---"
        line3 = f"Vol: {volume}%"

        draw.text((0, 0), line1[:21], font=_default_font, fill=255)
        draw.text((0, 11), line2[:21], font=_default_font, fill=255)
        draw.text((0, 22), line3[:21], font=_default_font, fill=255)

        display.image(image)
        display.show()

    except Exception as e:
        log.warning("Display update failed: %s", e)


def show_power_safe_display(display, boot_wait=False):
    """Show the fixed message used while the rear switch requests safe standby."""
    if display is None:
        return

    try:
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)

        if boot_wait:
            line1 = "Flip rear switch"
            line2 = "to ON"
            line3 = ""
        else:
            line1 = "OK to unplug"
            line2 = "Flip rear switch"
            line3 = "to ON to resume"

        draw.text((0, 0), line1[:21], font=_default_font, fill=255)
        draw.text((0, 11), line2[:21], font=_default_font, fill=255)
        draw.text((0, 22), line3[:21], font=_default_font, fill=255)

        display.image(image)
        display.show()
    except Exception as e:
        log.warning("Power-safe display update failed: %s", e)


def enter_power_safe_state(display, volume, boot_wait=False):
    """Stop radio activity, persist volume, flush writes, and show safe standby."""
    if boot_wait:
        log.warning("Rear switch is OFF at boot; waiting in power-loss standby")
    else:
        log.warning("Entering power-loss standby")

    # Stop audio and leave the ALSA/MPD software volume muted.
    mpc("stop")

    # At runtime, persist the user's actual volume before temporarily muting MPD.
    # At boot there is no new volume state to save; use the previously persisted value.
    if not boot_wait:
        save_state(volume)

    mpc("volume", "0")

    # Flush pending filesystem writes before telling the user it is OK to unplug.
    try:
        os.sync()
        log.info("Filesystem writes flushed for power-loss standby")
    except Exception as e:
        log.warning("Filesystem sync failed: %s", e)

    show_power_safe_display(display, boot_wait=boot_wait)


def wait_for_power_safe_release(display, volume, boot_wait=False):
    """Remain in power-safe standby until the maintained rear switch returns to ON."""
    enter_power_safe_state(display, volume, boot_wait=boot_wait)

    # The switch entered this function in the asserted/OFF state.  Use the same
    # 40 ms stable debounce rule before accepting a return to ON.
    rear_debounce = DebouncedValue(True)
    last_watchdog_notify = 0.0

    while not _shutdown:
        now = time.monotonic()
        raw_shutdown = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW
        change = rear_debounce.update(raw_shutdown, now)

        if change is not None:
            _old_state, new_state = change
            if not new_state:
                log.info("Rear switch -> ON; leaving power-loss standby")
                return True

        # radio.service has a systemd watchdog.  The controller must keep feeding
        # it while intentionally waiting in standby or systemd would restart us.
        if now - last_watchdog_notify >= WATCHDOG_NOTIFY_INTERVAL:
            _notify_watchdog()
            last_watchdog_notify = now

        time.sleep(POLL_INTERVAL)

    return False


# -----------------------------------------------------------------------------
# Playback
# -----------------------------------------------------------------------------


def resolve_latest_podcast_episode(feed_url):
    """Return the first playable podcast enclosure URL, or None on failure."""
    log.info("Fetching podcast feed: %s", feed_url)

    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "RadioFall2026/1.0"},
    )

    total_bytes = 0

    try:
        with urllib.request.urlopen(
            request,
            timeout=PODCAST_FETCH_TIMEOUT,
        ) as response:
            parser = ET.XMLPullParser(events=("end",))

            while True:
                chunk = response.read(PODCAST_READ_CHUNK)

                if not chunk:
                    break

                total_bytes += len(chunk)

                if total_bytes > PODCAST_MAX_BYTES:
                    log.error(
                        "No playable podcast episode found within "
                        "the first %d bytes",
                        PODCAST_MAX_BYTES,
                    )
                    return None

                parser.feed(chunk)

                for _event, item in parser.read_events():
                    if item.tag != "item":
                        continue

                    enclosure = item.find("enclosure")

                    if enclosure is None:
                        item.clear()
                        continue

                    audio_url = (enclosure.get("url") or "").strip()

                    if not audio_url.startswith(
                        ("http://", "https://")
                    ):
                        item.clear()
                        continue

                    title = (
                        item.findtext("title")
                        or "Untitled episode"
                    ).strip()

                    log.info(
                        "Latest podcast episode: %s",
                        title,
                    )

                    return audio_url

    except ET.ParseError as e:
        log.error(
            "Podcast feed contains invalid XML: %s",
            e,
        )
        return None

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as e:
        log.error(
            "Podcast feed request failed: %s",
            e,
        )
        return None

    log.error(
        "Podcast feed contains no playable enclosures"
    )
    return None

def play_stream(url):
    log.info("Playing stream: %s", url)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", url)
    mpc("play")


def play_podcast(feed_url):
    """Resolve and play the newest podcast episode from the beginning."""
    mpc("stop")

    audio_url = resolve_latest_podcast_episode(feed_url)

    if not audio_url:
        return False

    log.info("Starting podcast audio")
    play_stream(audio_url)

    if not _wait_for_playing():
        log.error("Podcast audio did not begin playing")
        mpc("stop")
        return False

    return True


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

    if stype == "podcast":
        url = station.get("url", "").strip()

        if not url:
            log.error("Podcast station '%s' has no url", name)
            return False

        return play_podcast(url)
    
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


def selected_bank_name(banks, bank_id):
    bank = banks.get(bank_id)
    if not isinstance(bank, dict):
        return "---"
    return bank.get("name", f"Bank {bank_id}")


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

    # GPIO setup comes before waiting for MPD so the rear standby switch and
    # OLED remain usable even if MPD is unavailable.
    GPIO.setmode(GPIO.BCM)

    for pin in BANK_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    for pin in STATION_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    for pin in VOLUME_PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(PLAY_STOP_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
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
    play_enabled = read_stable_switch_at_startup(PLAY_STOP_PIN, "play/stop")
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

    # If power is applied while the rear switch is OFF, do not enter radio mode.
    # Stay alive, muted, and responsive to that switch so the user can return to
    # normal operation without another power cycle.
    if shutdown_requested:
        # radio.service uses Type=notify.  Mark the controller ready before an
        # indefinite boot-time standby wait, then keep feeding the watchdog there.
        _notify_ready()

        if not wait_for_power_safe_release(display, volume, boot_wait=True):
            GPIO.cleanup()
            return

        shutdown_requested = False

        # Controls may have moved while we were waiting.  Re-read everything and
        # use the current volume-selector position only as the new relative reference.
        cur_volume_pos = read_stable_bcd_at_startup(VOLUME_PINS, "volume")
        cur_bank_pos = read_stable_bcd_at_startup(BANK_PINS, "bank")
        cur_station_pos = read_stable_bcd_at_startup(STATION_PINS, "station")
        play_enabled = read_stable_switch_at_startup(PLAY_STOP_PIN, "play/stop")

        bank_debounce = DebouncedValue(cur_bank_pos)
        station_debounce = DebouncedValue(cur_station_pos)
        volume_debounce = DebouncedValue(cur_volume_pos)
        play_debounce = DebouncedValue(play_enabled)
        shutdown_debounce = DebouncedValue(False)

        log.info(
            "Controls after standby: bank=%d station=%d volume=%d%% volume_pos=%d play=%s",
            cur_bank_pos,
            cur_station_pos,
            volume,
            cur_volume_pos,
            play_enabled,
        )

    if not wait_for_mpd():
        GPIO.cleanup()
        sys.exit(1)

    # Apply the persisted software volume only after the rear switch allows RUN.
    mpc("volume", str(volume))

    if play_enabled:
        station = select_station(banks, cur_bank_pos, cur_station_pos, True)
        if station is not None:
            playing_bank = cur_bank_pos
            playing_station = cur_station_pos
    else:
        log.info("Play/stop switch is STOP at startup")
        mpc("stop")

    update_display(
        display,
        cur_bank_pos,
        cur_station_pos,
        selected_bank_name(banks, cur_bank_pos),
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

            # Maintained play/stop switch.
            raw_play = GPIO.input(PLAY_STOP_PIN) == GPIO.LOW
            play_change = play_debounce.update(raw_play, now)

            if play_change is not None:
                _old_play, new_play = play_change
                play_enabled = new_play

                if play_enabled:
                    log.info("Play/stop switch -> PLAY")

                    # Always start the currently selected source fresh.
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
                    log.info("Play/stop switch -> STOP")
                    mpc("stop")
                    playing_bank = None
                    playing_station = None
                    watchdog_stop_since = 0.0

                display_dirty = True

            # Rear power-loss switch.  When asserted, stop all radio activity and
            # block here while polling only that maintained switch.
            raw_shutdown = GPIO.input(SHUTDOWN_PIN) == GPIO.LOW
            shutdown_change = shutdown_debounce.update(raw_shutdown, now)

            if shutdown_change is not None:
                _old_shutdown, new_shutdown = shutdown_change
                shutdown_requested = new_shutdown

                if shutdown_requested:
                    log.warning("Rear switch -> POWER-LOSS STANDBY")

                    if not wait_for_power_safe_release(display, volume, boot_wait=False):
                        break

                    # The rear switch is back ON.  Ignore all control movement that
                    # occurred during standby and establish fresh physical references.
                    shutdown_requested = False
                    cur_volume_pos = read_stable_bcd_at_startup(VOLUME_PINS, "volume")
                    cur_bank_pos = read_stable_bcd_at_startup(BANK_PINS, "bank")
                    cur_station_pos = read_stable_bcd_at_startup(STATION_PINS, "station")
                    play_enabled = read_stable_switch_at_startup(
                        PLAY_STOP_PIN, "play/stop"
                    )

                    bank_debounce = DebouncedValue(cur_bank_pos)
                    station_debounce = DebouncedValue(cur_station_pos)
                    volume_debounce = DebouncedValue(cur_volume_pos)
                    play_debounce = DebouncedValue(play_enabled)
                    shutdown_debounce = DebouncedValue(False)

                    # Restore the software volume saved on standby entry.
                    mpc("volume", str(volume))

                    playing_bank = None
                    playing_station = None
                    watchdog_stop_since = 0.0

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
                    else:
                        mpc("stop")

                    log.info(
                        "Radio resumed: bank=%d station=%d volume=%d%% volume_pos=%d play=%s",
                        cur_bank_pos,
                        cur_station_pos,
                        volume,
                        cur_volume_pos,
                        play_enabled,
                    )

                    update_display(
                        display,
                        cur_bank_pos,
                        cur_station_pos,
                        selected_bank_name(banks, cur_bank_pos),
                        selected_station_name(banks, cur_bank_pos, cur_station_pos),
                        volume,
                        play_enabled,
                        False,
                    )
                    last_display_update = time.monotonic()
                    display_dirty = False
                    state_dirty = False
                    watchdog_last_check = time.monotonic()
                    last_watchdog_notify = time.monotonic()
                    continue

                else:
                    log.info("Rear switch -> ON")
                    display_dirty = True


            # Stream watchdog.
            if play_enabled and now - watchdog_last_check >= WATCHDOG_INTERVAL:
                watchdog_last_check = now

                if playing_bank is not None and playing_station is not None:
                    _bank, stn = get_station(banks, playing_bank, playing_station)

                    if stn is not None and stn.get("type", "").strip().lower() == "stream":
                        status = mpc("status")

                        if "[playing]" not in status:
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
                    selected_bank_name(banks, cur_bank_pos),
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

    # Application shutdown (SIGTERM/SIGINT).  Rear-switch standby does not exit.
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
