#!/usr/bin/env python3
"""Raspberry Pi radio controller with OLED display.

Single script that handles:
- BCD rotary switch for volume control (10 positions)
- BCD rotary switch for station selection (wraps through flat list)
- Stop/start toggle switch
- 128x32 I2C OLED display (station name + volume)
- MPD playback via mpc commands
- Stream watchdog (auto-restarts dead streams)
- State persistence across power loss
- Systemd watchdog integration
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

# ── Paths ──────────────────────────────────────────────────
STATIONS_PATH = Path("/home/pi/stations.yaml")
AUDIO_ROOT = Path("/home/pi/audio")
AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac")
STATE_PATH = Path("/home/pi/state.json")
STATE_BACKUP_PATH = Path("/home/pi/state.backup.json")

# ── Hardware pin mappings ──────────────────────────────────
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

# OLED display I2C address (Adafruit 4440, SSD1306 128x32)
OLED_I2C_ADDR = 0x3C
OLED_WIDTH = 128
OLED_HEIGHT = 32

# ── Volume mapping ────────────────────────────────────────
VOLUME_MIN = 5
VOLUME_MAX = 100
DEFAULT_VOLUME = 25
VOLUME_STEP = 4  # Each knob position change increments/decrements by this amount

# ── Tuning ─────────────────────────────────────────────────
POLL_INTERVAL = 0.1       # Main loop sleep (seconds)
DEBOUNCE_TIME = 0.15      # Ignore switch changes faster than this
WATCHDOG_INTERVAL = 10.0  # Seconds between stream health checks
WATCHDOG_GRACE = 15.0     # Wait this long before restarting a dead stream
STATE_SAVE_INTERVAL = 5.0 # Seconds between state file writes
CONFIG_CHECK_INTERVAL = 30.0  # Seconds between stations.yaml mtime checks
WATCHDOG_NOTIFY_INTERVAL = 10.0  # Seconds between systemd watchdog keepalives
DISPLAY_UPDATE_INTERVAL = 0.5  # Seconds between OLED display refreshes

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("radio")

# ── Shutdown flag ────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutdown
    _shutdown = True
    log.info("Received signal %d, shutting down", signum)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Systemd watchdog ─────────────────────────────────────────

def _watchdog_enabled():
    """Check if systemd watchdog is configured."""
    usec = os.environ.get("WATCHDOG_USEC")
    return usec is not None and int(usec) > 0


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
        pass


def _notify_watchdog():
    """Send keepalive to systemd watchdog."""
    _sd_notify(b"WATCHDOG=1")


def _notify_ready():
    """Tell systemd we're ready (Type=notify)."""
    _sd_notify(b"READY=1")


# ── State persistence ────────────────────────────────────────

def _atomic_write_json(path: Path, payload: dict):
    """Atomically write JSON payload and fsync file + parent directory."""
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


def save_state(volume, station_index, play_enabled):
    """Atomically save state to disk and rotate a backup copy."""
    state = {
        "volume": volume,
        "station": station_index,
        "play_enabled": bool(play_enabled),
        "timestamp": int(time.time()),
    }
    try:
        _atomic_write_json(STATE_PATH, state)
        _atomic_write_json(STATE_BACKUP_PATH, state)
    except Exception as e:
        log.warning("Failed to save state: %s", e)


def _validate_state(data):
    """Validate loaded state and normalize values."""
    if not isinstance(data, dict):
        return None

    volume = data.get("volume")
    station = data.get("station")
    if not all(isinstance(v, int) for v in (volume, station)):
        return None

    if not (VOLUME_MIN <= volume <= VOLUME_MAX):
        return None
    if station < 0:
        return None

    return {
        "volume": volume,
        "station": station,
        "play_enabled": bool(data.get("play_enabled", True)),
        "timestamp": data.get("timestamp"),
    }


def load_state():
    """Load saved state from disk. Falls back to backup file if needed."""
    for path in (STATE_PATH, STATE_BACKUP_PATH):
        try:
            if not path.exists():
                continue
            with open(path) as f:
                data = json.load(f)
            validated = _validate_state(data)
            if validated:
                log.info("Restored state from %s: volume=%d station=%d",
                         path.name,
                         validated["volume"],
                         validated["station"])
                return validated
            log.warning("Invalid state data in %s", path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not load %s: %s", path, e)

    return None


# ── Helpers ────────────────────────────────────────────────

def mpc(*args):
    """Run an mpc command, return stdout. Swallow errors."""
    try:
        r = subprocess.run(
            ["mpc"] + list(args),
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            log.warning("mpc %s failed (code %d): %s", " ".join(args), r.returncode, stderr)
        return r.stdout.strip()
    except Exception as e:
        log.warning("mpc %s failed: %s", " ".join(args), e)
        return ""


def read_bcd(pins: dict) -> int:
    """Read 4-bit BCD value from GPIO pins (active LOW)."""
    val = 0
    if GPIO.input(pins["bit0"]) == GPIO.LOW: val += 1
    if GPIO.input(pins["bit1"]) == GPIO.LOW: val += 2
    if GPIO.input(pins["bit2"]) == GPIO.LOW: val += 4
    if GPIO.input(pins["bit3"]) == GPIO.LOW: val += 8
    return val


def load_stations():
    """Load stations.yaml and return flat list of station dicts."""
    if not STATIONS_PATH.exists():
        log.error("stations.yaml not found at %s", STATIONS_PATH)
        return []
    try:
        with open(STATIONS_PATH) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("Failed to load stations.yaml: %s", e)
        return []
    stations = data.get("stations", [])
    if not isinstance(stations, list):
        log.error("stations.yaml 'stations' key must be a list")
        return []
    return stations


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


# ── OLED Display ──────────────────────────────────────────

def init_display(i2c):
    """Initialize the SSD1306 128x32 OLED display."""
    try:
        display = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=OLED_I2C_ADDR)
        display.fill(0)
        display.show()
        log.info("OLED display ready at 0x%02x", OLED_I2C_ADDR)
        return display
    except Exception as e:
        log.error("OLED init failed: %s", e)
        return None


_default_font = ImageFont.load_default()


def update_display(display, station_index, station_name, volume, play_enabled):
    """Update the OLED display with station and volume info."""
    if display is None:
        return
    try:
        if not play_enabled:
            display.fill(0)
            display.show()
            return

        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)

        font = _default_font

        # Line 1: Station number
        station_num_str = f"Station {station_index + 1}"
        draw.text((0, 0), station_num_str, font=font, fill=255)

        # Line 2: Station name (truncated if needed)
        if station_name:
            # Truncate long names to fit ~21 chars at default font size
            display_name = station_name[:21]
        else:
            display_name = "---"
        draw.text((0, 11), display_name, font=font, fill=255)

        # Line 3: Volume
        vol_str = f"Vol: {volume}%"
        draw.text((0, 22), vol_str, font=font, fill=255)

        display.image(image)
        display.show()
    except Exception as e:
        log.warning("Display update failed: %s", e)


# ── Playback ───────────────────────────────────────────────

def play_stream(url):
    """Play an internet radio stream."""
    log.info("Playing stream: %s", url)
    mpc("clear")
    mpc("add", url)
    mpc("play")


def play_file(path_str):
    """Play a single local file on loop, starting at a random position."""
    resolved = _resolve_path(path_str)
    if not resolved.exists():
        log.error("File not found: %s", resolved)
        return

    rel = _mpd_relpath(resolved)
    log.info("Playing file (loop): %s", rel)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", rel)
    mpc("repeat", "on")
    mpc("play")
    # Wait for playback to start, then seek to a random position
    if _wait_for_playing():
        _seek_random()


def play_file_once(path_str):
    """Play a single local file from the beginning once, then stop."""
    resolved = _resolve_path(path_str)
    if not resolved.exists():
        log.error("File not found: %s", resolved)
        return

    rel = _mpd_relpath(resolved)
    log.info("Playing file once: %s", rel)
    mpc("clear")
    mpc("repeat", "off")
    mpc("single", "off")
    mpc("random", "off")
    mpc("add", rel)
    mpc("play")


def play_dir(path_str):
    """Play a directory from a random track and random position, then continue in order."""
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
    # Add all files via command-line arguments
    rel_paths = [_mpd_relpath(f) for f in files]
    try:
        subprocess.run(
            ["mpc", "add"] + rel_paths,
            text=True, timeout=30,
            capture_output=True,
        )
    except Exception as e:
        log.warning("mpc batch add failed: %s", e)

    start = random.randint(1, len(files))
    mpc("play", str(start))
    # Wait for playback, then seek to a random position in the first track.
    # MPD will continue to subsequent tracks from their beginnings.
    if _wait_for_playing():
        _seek_random()


def _resolve_path(raw: str) -> Path:
    """Resolve a station path relative to AUDIO_ROOT."""
    raw = raw.strip()
    candidate = Path(raw) if raw.startswith("/") else AUDIO_ROOT / raw
    try:
        candidate.resolve().relative_to(AUDIO_ROOT.resolve())
    except ValueError:
        log.error("Rejected path outside audio root: %s", candidate)
        return AUDIO_ROOT / "__invalid_path__"
    return candidate


def _mpd_relpath(path: Path) -> str:
    """Convert absolute path to MPD-relative path (relative to AUDIO_ROOT)."""
    try:
        return str(path.resolve().relative_to(AUDIO_ROOT.resolve()))
    except ValueError:
        log.error("Path outside audio root: %s", path)
        return ""


def _seek_random():
    """Seek to a random position in the current track."""
    output = mpc("status")
    for line in output.splitlines():
        if "/" in line and ":" in line and "%" in line:
            # Parse something like "   [playing] #1/1   0:05/3:42 (2%)"
            for token in line.split():
                if "/" in token and ":" in token:
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
    """Poll mpc status until [playing] appears, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = mpc("status")
        if "[playing]" in status:
            return True
        time.sleep(0.1)
    log.warning("Timed out waiting for [playing] state")
    return False


def play_station(station):
    """Play a station from the flat list. Returns True if successful."""
    if not isinstance(station, dict):
        return False

    name = station.get("name", "Unknown")
    stype = station.get("type", "").strip().lower()
    log.info("▶ %s [%s]", name, stype)

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


# ── Main loop ──────────────────────────────────────────────

def wait_for_mpd(retries=15, delay=2.0):
    """Block until MPD is reachable."""
    for i in range(retries):
        if _shutdown:
            return False
        r = subprocess.run(["mpc", "status"], capture_output=True, text=True)
        if r.returncode == 0:
            log.info("MPD ready (attempt %d/%d)", i + 1, retries)
            return True
        log.warning("Waiting for MPD... (%d/%d)", i + 1, retries)
        time.sleep(delay)
    log.error("MPD not available after %d attempts", retries)
    return False


def main():
    log.info("=" * 40)
    log.info("Radio controller starting")
    log.info("=" * 40)

    if not wait_for_mpd():
        sys.exit(1)

    # ── GPIO setup ──
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    all_pins = (
        list(STATION_PINS.values())
        + list(VOLUME_PINS.values())
        + [STOP_START_PIN]
    )
    for pin in all_pins:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # ── I2C / OLED display setup ──
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
    except Exception as e:
        log.error("I2C init failed: %s", e)
        sys.exit(1)

    display = init_display(i2c)

    # ── Load saved state (survives power loss) ──
    saved_state = load_state()

    # ── Load stations ──
    stations_list = load_stations()
    stations_mtime = STATIONS_PATH.stat().st_mtime if STATIONS_PATH.exists() else 0
    num_stations = len(stations_list)

    if num_stations == 0:
        log.error("No stations loaded — check stations.yaml")
        sys.exit(1)

    log.info("Loaded %d stations", num_stations)

    # ── Read initial switch positions ──
    cur_volume_pos = read_bcd(VOLUME_PINS)
    if cur_volume_pos > 9:
        cur_volume_pos = 0
    volume = max(VOLUME_MIN, min(VOLUME_MAX, VOLUME_MIN + cur_volume_pos * VOLUME_STEP))

    raw_station_pos = read_bcd(STATION_PINS)
    if raw_station_pos > 9:
        raw_station_pos = 0

    # Station index into the flat list — wraps using modulo
    cur_station_index = raw_station_pos % num_stations

    play_enabled = GPIO.input(STOP_START_PIN) == GPIO.LOW

    # Restore saved state if available (e.g. after power loss)
    if saved_state is not None:
        saved_volume = saved_state["volume"]
        saved_station = saved_state["station"]
        if 0 <= saved_station < num_stations:
            cur_station_index = saved_station
            log.info("Restored station %d from saved state", saved_station + 1)
        if VOLUME_MIN <= saved_volume <= VOLUME_MAX:
            volume = saved_volume
            log.info("Restored volume %d%% from saved state", saved_volume)
        play_enabled = saved_state.get("play_enabled", play_enabled)

    playing_station_index = -1
    last_station_switch_change = 0.0
    last_volume_switch_change = 0.0

    # Set initial volume
    mpc("volume", str(volume))

    # Play initial station
    if play_enabled and num_stations > 0:
        play_station(stations_list[cur_station_index])
        playing_station_index = cur_station_index
    elif not play_enabled:
        log.info("Stop/start switch is OFF at startup — stopped")
        mpc("stop")

    log.info("Initial: station=%d/%d volume=%d (pos %d) play=%s",
             cur_station_index + 1, num_stations, volume, cur_volume_pos, play_enabled)

    # Update display with initial state
    station_name = stations_list[cur_station_index].get("name", "Unknown") if num_stations > 0 else "---"
    update_display(display, cur_station_index, station_name, volume, play_enabled)

    # ── Watchdog state ──
    watchdog_last_check = 0.0
    watchdog_stop_since = 0.0

    # ── State persistence tracking ──
    last_state_save = 0.0
    state_dirty = True  # Save initial state on first opportunity

    # ── Config reload tracking ──
    last_config_check = 0.0

    # ── Systemd watchdog notify tracking ──
    last_watchdog_notify = 0.0

    # ── Display update tracking ──
    last_display_update = 0.0
    display_dirty = False

    # Tell systemd we're ready
    _notify_ready()

    # ── Main loop ──
    log.info("Entering main loop")
    while not _shutdown:
        try:
            now = time.monotonic()

            # ── Reload stations.yaml if it changed (throttled) ──
            if now - last_config_check >= CONFIG_CHECK_INTERVAL:
                last_config_check = now
                try:
                    mt = STATIONS_PATH.stat().st_mtime
                    if mt != stations_mtime:
                        new_stations = load_stations()
                        if new_stations:
                            stations_list = new_stations
                            num_stations = len(stations_list)
                            stations_mtime = mt
                            log.info("Reloaded stations.yaml (%d stations)", num_stations)
                            mpc("update")
                            # Clamp station indices to new list size
                            cur_station_index = cur_station_index % num_stations
                            if playing_station_index >= num_stations:
                                playing_station_index = cur_station_index
                            display_dirty = True
                        else:
                            log.warning("Ignoring stations.yaml reload due to invalid/empty station list")
                except FileNotFoundError:
                    pass

            # ── Read volume BCD switch ──
            raw_vol = read_bcd(VOLUME_PINS)
            new_vol_pos = raw_vol if 0 <= raw_vol <= 9 else cur_volume_pos

            if new_vol_pos != cur_volume_pos:
                if now - last_volume_switch_change >= DEBOUNCE_TIME:
                    last_volume_switch_change = now
                    delta = new_vol_pos - cur_volume_pos
                    if delta > 5:
                        delta -= 10
                    elif delta < -5:
                        delta += 10
                    cur_volume_pos = new_vol_pos
                    volume = max(VOLUME_MIN, min(VOLUME_MAX, volume + delta * VOLUME_STEP))
                    mpc("volume", str(volume))
                    log.info("Volume: pos %d → %d%% (step %+d)", cur_volume_pos, volume, delta * VOLUME_STEP)
                    state_dirty = True
                    display_dirty = True

            # ── Read station BCD switch ──
            raw_station = read_bcd(STATION_PINS)
            new_station_pos = raw_station if 0 <= raw_station <= 9 else raw_station_pos

            if new_station_pos != raw_station_pos:
                if now - last_station_switch_change >= DEBOUNCE_TIME:
                    last_station_switch_change = now
                    delta = new_station_pos - raw_station_pos
                    if delta > 5:
                        delta -= 10
                    elif delta < -5:
                        delta += 10
                    raw_station_pos = new_station_pos

                    new_station_index = (cur_station_index + delta) % num_stations if num_stations > 0 else 0
                    if new_station_index != cur_station_index:
                        log.info("Station: %d → %d (knob pos %d)",
                                 cur_station_index + 1, new_station_index + 1, raw_station_pos)
                        cur_station_index = new_station_index
                        state_dirty = True

                    # Play new station if it differs from what's currently playing
                    if play_enabled and num_stations > 0:
                        if cur_station_index != playing_station_index:
                            play_station(stations_list[cur_station_index])
                            playing_station_index = cur_station_index
                            watchdog_stop_since = 0.0
                            state_dirty = True
                    display_dirty = True

            # ── Stop/start switch ──
            new_play = GPIO.input(STOP_START_PIN) == GPIO.LOW
            if new_play != play_enabled:
                play_enabled = new_play
                if play_enabled:
                    log.info("Stop/start switch → ON")
                    if num_stations > 0:
                        play_station(stations_list[cur_station_index])
                        playing_station_index = cur_station_index
                else:
                    log.info("Stop/start switch → OFF")
                    mpc("stop")
                state_dirty = True
                display_dirty = True

            # ── Stream watchdog ──
            if play_enabled and now - watchdog_last_check >= WATCHDOG_INTERVAL:
                watchdog_last_check = now
                if 0 <= playing_station_index < num_stations:
                    stn = stations_list[playing_station_index]
                    if stn.get("type", "").strip().lower() == "stream":
                        status = mpc("status")
                        if "[playing]" not in status and "[paused]" not in status:
                            if watchdog_stop_since == 0.0:
                                watchdog_stop_since = now
                                log.warning("Stream appears stopped, waiting %.0fs...", WATCHDOG_GRACE)
                            elif now - watchdog_stop_since >= WATCHDOG_GRACE:
                                log.info("Watchdog: restarting stream (station %d)",
                                         playing_station_index + 1)
                                play_station(stations_list[playing_station_index])
                                watchdog_stop_since = 0.0
                        else:
                            watchdog_stop_since = 0.0

            # ── Update OLED display (throttled, only when changed) ──
            if display_dirty and now - last_display_update >= DISPLAY_UPDATE_INTERVAL:
                if num_stations > 0:
                    stn_name = stations_list[cur_station_index].get("name", "Unknown")
                else:
                    stn_name = "---"
                update_display(display, cur_station_index, stn_name, volume, play_enabled)
                last_display_update = now
                display_dirty = False

            # ── Save state to disk (throttled, only when changed) ──
            if state_dirty and now - last_state_save >= STATE_SAVE_INTERVAL:
                save_state(volume, cur_station_index, play_enabled)
                last_state_save = now
                state_dirty = False

            # ── Systemd watchdog keepalive (throttled) ──
            if now - last_watchdog_notify >= WATCHDOG_NOTIFY_INTERVAL:
                _notify_watchdog()
                last_watchdog_notify = now

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
            time.sleep(1.0)

    # ── Graceful shutdown ──
    log.info("Shutting down gracefully")
    save_state(volume, cur_station_index, play_enabled)
    if display:
        try:
            display.fill(0)
            display.show()
        except Exception:
            pass
    GPIO.cleanup()


if __name__ == "__main__":
    main()
