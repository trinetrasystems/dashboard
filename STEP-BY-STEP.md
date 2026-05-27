# Trinetra Monitor — Backend Setup Step-by-Step

This guide assumes you have the project unpacked locally. Layout:

```
trinetra-monitor/
├── frontend/        # static dashboard (no changes needed)
├── backend/         # FastAPI + SQLite + ingest
│   ├── app.py
│   ├── config.py
│   ├── schema.sql
│   └── requirements.txt
└── data/            # auto-created on first run
    ├── trinetra.db
    ├── incoming/    # drop .jpg files here, watcher picks them up
    ├── archive/     # processed images live here
    ├── heartbeats/  # one hb_<jetson>.txt per Jetson, updated every 30s
    └── dead-letter/ # bad-named files end up here
```

## Step 1 — Python venv + install deps (one-time)

```bash
cd backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Verify:
```bash
./venv/bin/python3 -c "import fastapi, uvicorn, watchdog; print('ok')"
```

## Step 2 — Inspect the schema (optional but recommended)

Open `backend/schema.sql` in your editor. The 5 tables:

- `detections` — every image that arrived
- `sessions` — open + closed presence sessions (the "in premises" data)
- `alerts` — loitering events fired (one per session)
- `whitelist` — badges that never trigger alerts
- `config` — runtime knobs (threshold, gap, exit cameras)

## Step 3 — Run the backend

```bash
cd backend
./venv/bin/python3 app.py
```

You should see:
```
[ingest] watching /…/data/incoming
[trinetra] startup complete · http://0.0.0.0:8000
```

The database file `data/trinetra.db` was just created — verify:

```bash
sqlite3 ../data/trinetra.db ".tables"
# alerts  detections  sessions  whitelist
```

## Step 4 — Open the dashboard

Browser → http://localhost:8000

The frontend is served by FastAPI from `../frontend/`. You should see the dashboard with no detections yet (empty premises table, zero KPIs).

The topbar will show `0 / 4 JETSONS ONLINE` because no heartbeat files exist yet — that's expected for local dev.

## Step 5 — Drop a test detection

In another terminal:

```bash
cd trinetra-monitor
touch "data/incoming/cam-entrance_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

Within a second:

- The file moves from `data/incoming/` → `data/archive/`
- A row appears in `detections` table
- A row appears in `sessions` table
- Dashboard auto-refreshes (every 5s) → RED-CIRCLE shows in "Currently in Premises"

Verify in DB:
```bash
sqlite3 data/trinetra.db "SELECT * FROM detections; SELECT * FROM sessions;"
```

## Step 6 — Trigger a loitering alert

Drop a file with a timestamp 20 minutes in the past (loiter threshold = 15min):

```bash
PAST=$(date -u -d '20 minutes ago' +%Y%m%d-%H%M%S)
touch "data/incoming/cam-parking_CYAN-PENTAGON_${PAST}.jpg"
# Then a "current" sighting to keep the session alive:
NOW=$(date -u +%Y%m%d-%H%M%S)
touch "data/incoming/cam-parking_CYAN-PENTAGON_${NOW}.jpg"
```

Within 30 seconds (the loitering checker runs every 30s), the red banner appears with an Ack button.

Click Ack → it greys out, count drops to 0.

## Step 7 — Test the whitelist

Open the Whitelist tab → add badge `BLACK-SHIELD` with reason "security guard".

Drop a detection for that badge:
```bash
touch "data/incoming/cam-entrance_BLACK-SHIELD_$(date +%Y%m%d-%H%M%S).jpg"
```

It shows up in Currently in Premises with a purple **WHITELIST** tag and is excluded from loitering alerts.

## Step 8 — Test exit-gate logic

Exit cameras are configured in `.env` (`TRINETRA_EXIT_CAMERAS`). Default is `cam-gate`.

Drop an exit detection:
```bash
touch "data/incoming/cam-gate_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

RED-CIRCLE disappears from the in-premises table → appears in the Logs page with an `EXIT` tag.

## Step 9 — Test the Logs page

Click "Logs & Charts" tab. You should see all detections from today.

Try:
- Search bar — type "RED" → only RED-CIRCLE rows
- Date picker — pick yesterday → empty (no data)
- Camera filter → pick "cam-gate" → only exit detections
- Click "Export CSV" → downloads a CSV of the filtered view

## Step 10 — Simulate a heartbeat (Jetson liveness)

The topbar shows `X / Y JETSONS ONLINE`. By default 4 expected: `central, edge-1, edge-2, edge-3`.

To simulate one being alive:
```bash
mkdir -p data/heartbeats
touch data/heartbeats/hb_edge-1.txt
# topbar now shows 1 / 4 JETSONS ONLINE
```

After 90 seconds without touching the file again, it goes back to offline. In production, each edge Jetson would `touch` its file every 30s and rsync it to central — same pipeline as detection files.

## Step 11 — Change loitering threshold (via `.env`)

Lower the loitering threshold to 5 minutes for faster testing. Edit `.env`:

```ini
TRINETRA_LOITER_MINUTES=5
```

Save and restart the server. The new threshold takes effect immediately.

---

# What's running

When `app.py` starts, four things happen concurrently:

1. **FastAPI** serves HTTP/WebSocket on port 8000
2. **Watchdog observer** watches `data/incoming/` (inotify-based, ~zero CPU)
3. **Loitering check loop** runs every 30s, fires alerts
4. **Static file server** serves the frontend from `../frontend/`

All in one process. Resource usage is ~50MB RAM idle.

# Useful commands

```bash
# Inspect database
sqlite3 data/trinetra.db
> .tables
> SELECT * FROM sessions WHERE closed=0;
> SELECT badge, COUNT(*) FROM detections GROUP BY badge;

# Live tail logs
tail -f /path/to/your/log    # if you redirect output

# Test API directly
curl http://localhost:8000/api/stats
curl http://localhost:8000/api/sessions/active
curl http://localhost:8000/api/detections?limit=10
curl http://localhost:8000/api/whitelist

# Add whitelist via curl
curl -X POST http://localhost:8000/api/whitelist \
  -H 'Content-Type: application/json' \
  -d '{"badge":"TEST-CIRCLE","reason":"testing"}'
```

# Notification setup (optional)

Edit a `.env` file or `export` before running:

```bash
export TELEGRAM_TOKEN=123456:ABCDEF...
export TELEGRAM_CHAT_ID=-1001234567890
export SMTP_USER=you@gmail.com
export SMTP_PASS=app_password_here
export SMTP_TO=alerts@company.com
./venv/bin/python3 app.py
```

Get a Telegram bot token: chat with `@BotFather`, `/newbot`, save token. Get chat ID:
```bash
curl https://api.telegram.org/bot<TOKEN>/getUpdates
```

# When to use dummy data fallback

In `frontend/js/api.js`, set:
```javascript
const USE_DUMMY_DATA = true;
```

The frontend then uses in-memory dummy data, ignoring the backend. Useful for UI work without running the server.

---

# Next: Docker

Once the local dev workflow above is working, see `DOCKER.md` (next file) for containerizing and shipping to Jetson.
