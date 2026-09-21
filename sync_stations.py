#!/usr/bin/env python3
"""
Safely update RadioFall stations.yaml from GitHub.

The updater:
- downloads only stations.yaml
- rejects malformed YAML and duplicate keys
- validates banks, stations, station types, URLs, and local paths
- verifies referenced local audio exists
- leaves the current configuration untouched if anything is wrong
- keeps one backup of the previous configuration
- atomically replaces /home/pi/stations.yaml only after validation succeeds

Running with:

    sync_stations.py --check FILE

validates a local file without downloading or installing anything.
"""

import argparse
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml


REMOTE_URL = (
    "https://raw.githubusercontent.com/"
    "jodazsa/radiofall2026/main/stations.yaml"
)

DEST_PATH = Path("/home/pi/stations.yaml")
BACKUP_PATH = Path("/home/pi/stations.yaml.backup")
AUDIO_ROOT = Path("/home/pi/audio")

DOWNLOAD_TIMEOUT = 20
MAX_DOWNLOAD_BYTES = 1024 * 1024

VALID_TYPES = {
    "stream",
    "podcast",
    "file",
    "file_once",
    "single_file",
    "dir",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("radio-stations-sync")


class UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML loader that rejects duplicate mapping keys."""


def _construct_mapping(loader, node, deep=False):
    mapping = {}

    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)

        if key in mapping:
            raise yaml.YAMLError(f"Duplicate YAML key: {key!r}")

        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value

    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml_bytes(content):
    """Parse YAML while rejecting duplicate mapping keys."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("stations.yaml is not valid UTF-8") from exc

    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Top-level YAML value must be a mapping")

    return data


def resolve_local_path(raw_path):
    """Resolve a station path under /home/pi/audio."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Local station has an empty path")

    raw_path = raw_path.strip()

    candidate = AUDIO_ROOT / raw_path

    try:
        resolved = candidate.resolve()
        resolved.relative_to(AUDIO_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Local path escapes audio directory: {raw_path}"
        ) from exc

    return resolved


def validate_station(bank_id, station_id, station):
    """Validate one station definition."""
    if not isinstance(station, dict):
        raise ValueError(
            f"Bank {bank_id} station {station_id} must be a mapping"
        )

    name = station.get("name")

    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"Bank {bank_id} station {station_id} has no valid name"
        )

    station_type = station.get("type")

    if not isinstance(station_type, str):
        raise ValueError(
            f"Bank {bank_id} station {station_id} has no valid type"
        )

    station_type = station_type.strip().lower()

    if station_type not in VALID_TYPES:
        raise ValueError(
            f"Bank {bank_id} station {station_id} has "
            f"unknown type {station_type!r}"
        )

    if station_type in ("stream", "podcast"):
        url = station.get("url")

        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"Bank {bank_id} station {station_id} "
                "is a stream but has no URL"
            )
        
        url = url.strip()

        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"Bank {bank_id} station {station_id} "
                f"has invalid {station_type} URL"
            )
        return

    raw_path = station.get("path")

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(
            f"Bank {bank_id} station {station_id} "
            "is local but has no path"
        )

    resolved = resolve_local_path(raw_path)

    if station_type == "dir":
        if not resolved.is_dir():
            raise ValueError(
                f"Bank {bank_id} station {station_id} "
                f"directory does not exist: {raw_path}"
            )
    else:
        if not resolved.is_file():
            raise ValueError(
                f"Bank {bank_id} station {station_id} "
                f"file does not exist: {raw_path}"
            )


def validate_config(data):
    """Validate the complete stations.yaml structure."""
    banks = data.get("banks")

    if not isinstance(banks, dict):
        raise ValueError("'banks' must be a mapping")

    # The physical bank selector has positions 0 through 9.
    for required_bank in range(10):
        if required_bank not in banks:
            raise ValueError(
                f"Required bank {required_bank} is missing"
            )

    for bank_id, bank in banks.items():
        if not isinstance(bank_id, int) or bank_id < 0:
            raise ValueError(f"Invalid bank number: {bank_id!r}")

        if not isinstance(bank, dict):
            raise ValueError(f"Bank {bank_id} must be a mapping")

        bank_name = bank.get("name")

        if not isinstance(bank_name, str) or not bank_name.strip():
            raise ValueError(f"Bank {bank_id} has no valid name")

        stations = bank.get("stations")

        if not isinstance(stations, dict):
            raise ValueError(
                f"Bank {bank_id} stations must be a mapping"
            )

        # Physical station positions 0 through 9 must remain usable.
        # Additional station numbers are allowed for future use.
        for required_station in range(10):
            if required_station not in stations:
                raise ValueError(
                    f"Bank {bank_id} is missing "
                    f"station {required_station}"
                )

        for station_id, station in stations.items():
            if not isinstance(station_id, int) or station_id < 0:
                raise ValueError(
                    f"Bank {bank_id} has invalid station "
                    f"number {station_id!r}"
                )

            validate_station(bank_id, station_id, station)

    return len(banks)


def download_candidate():
    """Download stations.yaml without modifying the installed copy."""
    request = urllib.request.Request(
        REMOTE_URL,
        headers={"User-Agent": "RadioFall2026-stations-sync"},
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            content = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise RuntimeError(f"Download failed: {exc}") from exc

    if len(content) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("Downloaded stations.yaml is unexpectedly large")

    if not content:
        raise RuntimeError("Downloaded stations.yaml is empty")

    return content


def atomic_install(content):
    """Install validated content without exposing a partial file."""
    DEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    if DEST_PATH.exists():
        current = DEST_PATH.read_bytes()

        if current == content:
            log.info("stations.yaml is already current")
            return False

        shutil.copy2(DEST_PATH, BACKUP_PATH)

    fd, temp_name = tempfile.mkstemp(
        prefix=".stations.",
        suffix=".yaml.tmp",
        dir=str(DEST_PATH.parent),
    )

    temp_path = Path(temp_name)

    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chmod(temp_path, 0o644)
        os.replace(temp_path, DEST_PATH)

        dir_fd = os.open(
            str(DEST_PATH.parent),
            os.O_RDONLY,
        )

        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    finally:
        if temp_path.exists():
            temp_path.unlink()

    log.info("Installed new stations.yaml")
    return True


def check_file(path):
    """Validate a local stations.yaml without installing it."""
    content = Path(path).read_bytes()
    data = load_yaml_bytes(content)
    bank_count = validate_config(data)

    log.info(
        "Validation successful: %d configured banks",
        bank_count,
    )


def update_from_github():
    """Download, validate, and atomically install stations.yaml."""
    log.info("Checking GitHub for stations.yaml")

    content = download_candidate()

    data = load_yaml_bytes(content)
    bank_count = validate_config(data)

    log.info(
        "Downloaded configuration passed validation: %d banks",
        bank_count,
    )

    atomic_install(content)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--check",
        metavar="FILE",
        help="validate a local stations.yaml without installing it",
    )

    args = parser.parse_args()

    try:
        if args.check:
            check_file(args.check)
        else:
            update_from_github()

    except Exception as exc:
        log.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())