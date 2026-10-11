#!/usr/bin/env bash

set -euo pipefail

INTERFACE="${WIFI_SETUP_INTERFACE:-wlan0}"
WAIT_SECONDS="${WIFI_SETUP_WAIT_SECONDS:-60}"
POLL_SECONDS=3

PORTAL_SSID="${WIFI_SETUP_SSID:-RadioFall-Setup}"
PORTAL_PASSPHRASE="${WIFI_SETUP_PASSPHRASE:-RadioFall26}"

WIFI_CONNECT="/usr/local/bin/wifi-connect"
UI_DIR="/opt/wifi-connect/ui"

log() {
    echo "radio-wifi-setup: $*"
}

wifi_connected() {
    local state

    state="$(
        nmcli -g GENERAL.STATE device show "$INTERFACE" 2>/dev/null \
            || true
    )"

    [[ "$state" == 100* ]]
}

if [[ ! -x "$WIFI_CONNECT" ]]; then
    log "ERROR: $WIFI_CONNECT is not installed"
    exit 1
fi

if [[ ! -f "$UI_DIR/index.html" ]]; then
    log "ERROR: WiFi Connect UI not found at $UI_DIR"
    exit 1
fi

if wifi_connected; then
    log "$INTERFACE is already connected; setup portal not needed"
    exit 0
fi

log "Waiting up to ${WAIT_SECONDS}s for saved Wi-Fi..."

deadline=$((SECONDS + WAIT_SECONDS))

while (( SECONDS < deadline )); do
    sleep "$POLL_SECONDS"

    if wifi_connected; then
        log "$INTERFACE connected; setup portal not needed"
        exit 0
    fi
done

log "No Wi-Fi connection found"
log "Starting setup portal: $PORTAL_SSID"

nmcli radio wifi on

exec "$WIFI_CONNECT" \
    --portal-interface "$INTERFACE" \
    --portal-ssid "$PORTAL_SSID" \
    --portal-passphrase "$PORTAL_PASSPHRASE" \
    --ui-directory "$UI_DIR"
