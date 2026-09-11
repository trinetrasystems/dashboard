#!/bin/bash
# Live view of push-notification activity while testing the mobile app.
#   ./watch-push.sh
# Shows registered devices and the last few send attempts, refreshing every 3s.
DB="$(dirname "$0")/data/trinetra.db"

while true; do
  clear
  echo "════ TRINETRA PUSH MONITOR ═══════════════════════ $(date '+%H:%M:%S') ════"
  echo
  echo "── NOTIFICATION USERS ──"
  sqlite3 -header -column "$DB" \
    "SELECT user_id, COALESCE(name,'') AS name,
            CASE notification_enabled WHEN 1 THEN 'yes' ELSE 'NO' END AS enabled,
            CASE WHEN firebase_uid IS NOT NULL THEN 'firebase' ELSE 'passcode' END AS login
     FROM users ORDER BY user_id;" 2>/dev/null | sed 's/^/  /'

  echo
  echo "── REGISTERED DEVICES ──"
  sqlite3 -header -column "$DB" \
    "SELECT user_id, platform, COALESCE(browser,'') AS device,
            CASE is_active WHEN 1 THEN 'active' ELSE 'off' END AS status,
            substr(fcm_token,1,18)||'…' AS token,
            datetime(last_seen_at,'localtime') AS last_seen
     FROM user_devices ORDER BY last_seen_at DESC LIMIT 8;" 2>/dev/null | sed 's/^/  /'
  [ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM user_devices;' 2>/dev/null)" = "0" ] &&
    echo "  (none yet — waiting for the app to call /register-device)"

  echo
  echo "── LAST 6 SEND ATTEMPTS ──"
  sqlite3 -header -column "$DB" \
    "SELECT datetime(created_at,'localtime') AS at, user_id, status,
            sent_count AS sent, failed_count AS failed, substr(COALESCE(detail,''),1,34) AS detail
     FROM notification_log ORDER BY id DESC LIMIT 6;" 2>/dev/null | sed 's/^/  /'
  [ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM notification_log;' 2>/dev/null)" = "0" ] &&
    echo "  (none yet)"

  echo
  echo "── SERVER LOG (push lines) ──"
  grep -E "^\[push\]" "$(dirname "$0")/trinetra.log" 2>/dev/null | tail -4 | sed 's/^/  /'
  echo
  echo "Ctrl-C to exit"
  sleep 3
done
