#!/usr/bin/env bash
# Start a virtual display, then run the join-layer debug runner on it FROM SOURCE.
# src/ and scripts/ are mounted live from the host (make debug) — tsx runs the
# TypeScript directly, so a host edit + re-run is instant, no rebuild. The image
# bakes only the reproducible environment (Xvfb + humanized X11 + noVNC + deps).
# startDebugView() auto-spawns x11vnc + websockify(noVNC:6080) because DISPLAY is set.
set -e

: "${MEETING_URL:?set MEETING_URL to a meet.google.com or Teams meeting link}"

Xvfb :99 -screen 0 1920x1080x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
export DEBUG_ADMISSION=1   # rich admission DOM dumps for oracle debugging
# noVNC must listen on the container interface for `docker run -p 127.0.0.1:6080:6080` to reach it
# (the Makefile publishes on HOST loopback only). x11vnc itself stays loopback + password-protected;
# the password is printed by debug-join.ts.
export VEXA_VNC_LISTEN_HOST="${VEXA_VNC_LISTEN_HOST:-0.0.0.0}"
( command -v fluxbox >/dev/null && fluxbox >/tmp/fluxbox.log 2>&1 & ) || true

for i in $(seq 1 20); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.25; done

echo "[entrypoint] DISPLAY :99 up — running join layer from source (tsx)"
exec npx tsx scripts/debug-join.ts "$MEETING_URL"
