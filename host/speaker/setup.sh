#!/usr/bin/env bash
# Bluetooth speaker for the perimeter alerts, on the Jetson HOST (root): bluetooth on, bluez-alsa, the agent service.
#   sudo bash setup.sh [data-dir]          (install.sh runs it; safe to run again)
# The Jetson optimisation turns Bluetooth off (unused); this turns it back on for the alert speaker only.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); DATA=${1:-/home/nvidia/PERIMETER_data}
log() { echo "[speaker] $*"; }
if [ ! -d /sys/class/bluetooth ] || [ -z "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
  log "no Bluetooth radio on this Jetson — speaker not set up (the dashboard beeps in the browser as before)"; exit 0
fi
if ! dpkg -s bluez >/dev/null 2>&1 || ! dpkg -s bluez-alsa-utils >/dev/null 2>&1; then
  log "installing bluez + bluez-alsa-utils"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bluez bluez-alsa-utils libasound2-plugin-bluez >/dev/null \
    || { log "could not install bluez-alsa (no internet?) — run again when online"; exit 0; }
fi
systemctl unmask bluetooth.service >/dev/null 2>&1 || true
systemctl enable --now bluetooth.service >/dev/null
BA=$(systemctl list-unit-files --no-legend 'bluez-alsa.service' 'bluealsa.service' 2>/dev/null | awk '{print $1}' | head -1)
[ -n "$BA" ] && systemctl enable --now "$BA" >/dev/null 2>&1 && log "audio: $BA running" || log "NOTE: no bluez-alsa service found"
rfkill unblock bluetooth 2>/dev/null || true
install -D -m 755 "$HERE/perimeter-speaker.py" /usr/local/lib/trinetra/perimeter-speaker.py
sed "s#^Environment=PERIMETER_DATA=.*#Environment=PERIMETER_DATA=$DATA#" "$HERE/perimeter-speaker.service" > /etc/systemd/system/perimeter-speaker.service
mkdir -p "$DATA/speaker/requests"; chmod 700 "$DATA/speaker"
systemctl daemon-reload
systemctl enable perimeter-speaker.service >/dev/null
systemctl restart perimeter-speaker.service
sleep 3
systemctl is-active --quiet perimeter-speaker.service && log "running (Alerts page → Speaker)" || { log "FAILED"; journalctl -u perimeter-speaker -n 20 --no-pager; exit 1; }
