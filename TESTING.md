# Trinetra Monitor — Complete Testing Guide

> **All features verified end-to-end with 67 automated checks before this release.**
> Test sequentially — each builds on the previous.

---

## 📋 What you need

- **Terminal 1** — runs the server (keep open throughout)
- **Terminal 2** — drops test files
- **Terminal 3** — inspects database (optional)
- **Browser tab** — http://localhost:8000

---

# PART A — One-time install

## 🐧 Linux / Jetson

```bash
tar -xzf trinetra-monitor-full.tar.gz
cd trinetra-monitor-full/backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 🪟 Windows

```powershell
# Extract trinetra-monitor-full.zip
cd trinetra-monitor-full\backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` errors with "running scripts is disabled":
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

---

# PART B — Optional: create `.env`

`.env` is **optional**. Without it, defaults apply (data in `./data`, port 8000). Skip for first-time testing.

### Linux
```bash
cd ..   # back to project root
cp .env.example .env
nano .env
```

### Windows
```powershell
cd ..
Copy-Item .env.example .env
notepad .env
```

You'll change one value later (`TRINETRA_LOITER_MINUTES=2` for TEST 16). Defaults are fine to start.

---

# PART C — Start the server (Terminal 1)

### Linux
```bash
cd trinetra-monitor-full/backend
./venv/bin/python3 app.py
```

### Windows
```powershell
cd trinetra-monitor-full\backend
.\venv\Scripts\python.exe app.py
```

**Expected output:**
```
[config] no .env at .../.env — using defaults (this is fine for first run)
[config] PROJECT_ROOT  = /path/to/trinetra-monitor-full
[config] DATA_DIR      = /path/to/trinetra-monitor-full/data
[config] DB_PATH       = .../data/trinetra.db
[config] INCOMING_DIR  = .../data/incoming
[config] ARCHIVE_DIR   = .../data/archive
[config] FRONTEND_DIR  = .../frontend
[config] CLASSES_FILE  = .../backend/classes.txt
[config] JETSONS       = ['central', 'edge-1', 'edge-2', 'edge-3']
[config] LOITER_MIN    = 15 min
[config] GAP_MIN       = 5 min
[config] EXIT_CAMERAS  = ['cam-gate']
[ingest] watching .../data/incoming
[trinetra] startup complete · http://0.0.0.0:8000
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

✅ **Check the `[config]` lines** — confirm paths look right for your machine.

**Open browser:** http://localhost:8000

**You should see:**
- Topbar: `0 / 4 JETSONS ONLINE`, `0 LOITERING`, `0 IN PREMISES`
- 3 KPI cards all show `0`
- "Today's Detections" chart empty
- "Currently in Premises": `no active sessions`
- "Live Notifications" panel on right: `waiting for events…`

---

# PART D — Run the 17 tests

Terminal 2 commands assume you're in the **project root** (`trinetra-monitor-full/`), not `backend/`.

---

## TEST 1 — Drop a single detection

### Linux
```bash
cd trinetra-monitor-full
touch "data/incoming/cam-entrance_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
cd trinetra-monitor-full
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_RED-CIRCLE_$ts.jpg"
```

**Wait 5 seconds. Browser:**
- "Detections Today" KPI: 0 → 1 ✓
- "Currently in Premises" KPI: 0 → 1 ✓
- Premises table: `RED-CIRCLE` | now | duration `00:0X` (green) | `OK` | 1 sighting ✓
- Today's Detections chart: small bar at current hour ✓
- Live Notifications panel: new entry for RED-CIRCLE ✓

**Disk:**
```bash
ls data/incoming/     # empty
ls data/archive/      # contains the file
```

✅ File watcher works, archive logic works, frontend polls every 5s.

---

## TEST 2 — Click row → enhanced badge detail modal

Browser → click **RED-CIRCLE row** in premises table.

**Modal opens with:**
- Title: `RED-CIRCLE`
- Subtitle: `cam-entrance · HH:MM:SS · ENTRY`
- Image area: "⊘ NO IMAGE AVAILABLE" (because `touch` creates 0-byte files)
- "Recent sightings (1)" header
- One log entry, highlighted with cyan left border ✓

Close with ✕ Close button or click outside.

✅ Badge detail modal works.

---

## TEST 3 — Same badge again → session extends + history grows

### Linux
```bash
sleep 5
touch "data/incoming/cam-parking_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
sleep 5
touch "data/incoming/cam-entrance_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
Start-Sleep 5
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-parking_RED-CIRCLE_$ts.jpg"
Start-Sleep 5
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_RED-CIRCLE_$ts.jpg"
```

**Browser:**
- Same RED-CIRCLE row, `Sightings` 1 → 2 → 3
- `Last Seen` updates with each new camera
- Duration ticks up live
- Notifications panel: 3 entries for RED-CIRCLE now

**Click RED-CIRCLE row → modal opens:**
- Header: `Recent sightings (3)`
- 3 log entries visible

**Click the SECOND entry in the list:**
- Cyan border moves from row 1 to row 2 ✓
- Subtitle updates to that detection's details ✓

✅ Session extension + history endpoint + click-to-swap works.

---

## TEST 4 — Different badge → new row

### Linux
```bash
touch "data/incoming/cam-parking_BLUE-SQUARE_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-parking_BLUE-SQUARE_$ts.jpg"
```

**Browser:**
- New row: BLUE-SQUARE
- "In Premises" KPI: 1 → 2
- Sorted longest-first (RED-CIRCLE on top)

✅ Distinct badges create distinct sessions.

---

## TEST 5 — Bad filename → dead-letter

### Linux
```bash
touch "data/incoming/CAM_ENTRANCE_RED_CIRCLE_20260527-103000.jpg"
```

### Windows
```powershell
New-Item -ItemType File -Path "data\incoming\CAM_ENTRANCE_RED_CIRCLE_20260527-103000.jpg"
```

**Terminal 1:**
```
[ingest] bad filename → dead-letter: CAM_ENTRANCE_RED_CIRCLE_20260527-103000.jpg
```

**Disk:**
```bash
ls data/dead-letter/    # contains the file
ls data/incoming/       # empty
```

✅ Filename validator catches files with extra underscores.

---

## TEST 6 — Inspect the database

### Linux
```bash
sqlite3 data/trinetra.db
```

```sql
.tables
-- alerts  detections  sessions  whitelist  (no 'config' table)

SELECT id, camera, badge, type, datetime(timestamp,'localtime') AS local_time FROM detections;
-- 4 rows

SELECT id, badge, total_sightings, last_camera, closed FROM sessions;
-- 2 rows, both closed=0

.quit
```

### Windows
Install **DB Browser for SQLite** from https://sqlitebrowser.org → open `data\trinetra.db`.

✅ Schema correct. Timestamps stored in UTC, displayable in local time.

---

## TEST 7 — Exit camera closes session

`cam-gate` is the configured exit camera (from `TRINETRA_EXIT_CAMERAS` in `.env`).

### Linux
```bash
touch "data/incoming/cam-gate_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-gate_RED-CIRCLE_$ts.jpg"
```

**Browser:**
- RED-CIRCLE disappears from premises table ✓
- "In Premises": 2 → 1
- Only BLUE-SQUARE remains

**Click Logs & Charts tab:**
- See the exit detection at top with purple **EXIT** tag

**DB:**
```sql
SELECT id, badge, closed FROM sessions;
-- session 1 (RED-CIRCLE): closed=1
-- session 2 (BLUE-SQUARE): closed=0
```

✅ Exit gate logic works.

---

## TEST 8 — Trigger a loitering alert

Past timestamp + current timestamp keeps session alive past the threshold.

### Linux
```bash
PAST=$(date -d '20 minutes ago' +%Y%m%d-%H%M%S)
NOW=$(date +%Y%m%d-%H%M%S)
touch "data/incoming/cam-parking_CYAN-PENTAGON_${PAST}.jpg"
touch "data/incoming/cam-parking_CYAN-PENTAGON_${NOW}.jpg"
```

### Windows
```powershell
$past = (Get-Date).AddMinutes(-20).ToString('yyyyMMdd-HHmmss')
$now  = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-parking_CYAN-PENTAGON_$past.jpg"
New-Item -ItemType File -Path "data\incoming\cam-parking_CYAN-PENTAGON_$now.jpg"
```

**Wait up to 30 seconds** (loitering check runs every 30s).

**Browser:**
- Red banner: `LOITERING DETECTED · CYAN-PENTAGON · 20:XX` ✓
- `✓ Ack` button visible ✓
- "Active Loitering Alerts" KPI: 0 → 1 ✓
- Topbar pill "1 LOITERING" turns red ✓
- CYAN-PENTAGON row shows red **LOITERING** status ✓

✅ Loitering check fires. Duration is correct (no timezone bug).

---

## TEST 9 — Acknowledge the alert

Browser → click **✓ Ack** on CYAN-PENTAGON banner.

- Card greys out, ack button gone ✓
- Card text changes to "acked by operator" ✓
- "Active Loitering Alerts": 1 → 0 ✓
- Topbar pill: red → grey ✓
- CYAN-PENTAGON still in premises table (alert muted, not session closed) ✓

**DB:**
```sql
SELECT id, badge, acknowledged, acknowledged_by FROM alerts;
-- acknowledged=1, acknowledged_by=operator
```

✅ Ack works.

---

## TEST 10 — Whitelist via dropdown (classes.txt)

Click **Whitelist** tab.

- Dropdown shows "— select a badge —"
- Click → 13 badges from `backend/classes.txt`

**Add a badge:**
- Pick `BLUE-SQUARE`
- Type reason: `testing whitelist`
- Click **+ Add Badge**

- Dropdown clears, BLUE-SQUARE moves to list ✓
- BLUE-SQUARE removed from dropdown ✓

**Live tab:**
- BLUE-SQUARE row shows purple **WHITELIST** tag ✓
- Won't trigger loitering alerts ✓

**DB:**
```sql
SELECT * FROM whitelist;
-- BLUE-SQUARE | testing whitelist | <timestamp>
```

✅ classes.txt → dropdown wiring works.

---

## TEST 11 — Live-edit classes.txt

Open `backend/classes.txt` in any editor. Add:
```
PURPLE-WAVE
```

Save. **Don't restart the server.**

Refresh Whitelist page (F5):
- Open dropdown → `PURPLE-WAVE` appears ✓

✅ classes.txt is read on every API call, no restart needed.

---

## TEST 12 — Auto-discovery of unknown badges

Drop a detection for a badge NOT in `classes.txt`:

### Linux
```bash
touch "data/incoming/cam-entrance_MYSTERY-BADGE_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_MYSTERY-BADGE_$ts.jpg"
```

**Browser:**
- New row in premises: MYSTERY-BADGE (accepted, not rejected)
- Whitelist tab dropdown: `MYSTERY-BADGE (auto)` — the `(auto)` tag means auto-discovered

✅ Lenient mode for unknown badges, visually flagged.

---

## TEST 13 — Logs page filters + CSV export

Click **Logs & Charts** tab.

**Try filters:**

1. **Search** — type `RED` → only RED-CIRCLE rows ✓
2. **Camera** — pick `cam-gate` → only exit detections ✓
3. **Badge dropdown** — see ALL 13+ badges from classes.txt ✓
4. **Date picker** — pick yesterday → empty table ✓
5. **Clear filters** → all today's data returns

**Click `⬇ Export CSV`:**
- Downloads `trinetra-detections-YYYYMMDD-HHMMSS.csv` ✓
- Open in Excel/Notepad → comma-separated log ✓

✅ Filters, search, CSV all work.

---

## TEST 14 — Click log → image modal

In Logs page, **click any row**:
- Image modal opens with badge name + camera + time + type
- Image area: "⊘ NO IMAGE AVAILABLE" (test files are 0-byte)
- ✕ Close to dismiss

**Live tab, click any Live Notification:**
- Same simple modal opens

✅ Both clickable, both open image modal.

---

## TEST 14.5 — The badge hand-off scenario (NEW)

**This is the key new test.** Verifies that the same badge given to a different person after exit creates a **separate visit** in the system.

**Visit 1 (Person A):**

### Linux
```bash
# Person A enters with GOLD-STAR
touch "data/incoming/cam-entrance_GOLD-STAR_$(date +%Y%m%d-%H%M%S).jpg"
sleep 3
touch "data/incoming/cam-parking_GOLD-STAR_$(date +%Y%m%d-%H%M%S).jpg"
sleep 3
# Person A exits at the gate (returns badge to security)
touch "data/incoming/cam-gate_GOLD-STAR_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_GOLD-STAR_$ts.jpg"
Start-Sleep 3
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-parking_GOLD-STAR_$ts.jpg"
Start-Sleep 3
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-gate_GOLD-STAR_$ts.jpg"
```

**Click the Visits tab:**
- One row appears for GOLD-STAR
- Status: **COMPLETED** (purple tag)
- Exited column shows the gate time, duration ~6s, sightings 3, cameras chip shows entry/parking/gate counts ✓

**Now Person B gets the same badge from security and enters:**

### Linux
```bash
sleep 5
touch "data/incoming/cam-entrance_GOLD-STAR_$(date +%Y%m%d-%H%M%S).jpg"
sleep 3
touch "data/incoming/cam-perimeter_GOLD-STAR_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
Start-Sleep 5
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_GOLD-STAR_$ts.jpg"
Start-Sleep 3
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-perimeter_GOLD-STAR_$ts.jpg"
```

**Refresh the Visits page:**
- **Two rows** now appear for GOLD-STAR ✓
- First row (newer): GOLD-STAR | entered just now | **still here** | duration ~3s | 2 sightings | **IN PREMISES** ✓
- Second row (older): GOLD-STAR | entered 6s ago | exited X seconds ago | duration | 3 sightings | **COMPLETED** ✓

**Click row #1 (the new visit):**
- Modal opens: `GOLD-STAR · Visit #2`
- Subtitle shows the date range and "still here"
- Timeline shows only the 2 detections from Person B (not Person A's 3)

**Click row #2 (the original visit):**
- Modal opens: `GOLD-STAR · Visit #1`
- Timeline shows only the 3 detections from Person A's visit (entry, parking, exit)

**Verify in DB:**
```sql
SELECT id, badge, datetime(first_seen,'localtime') AS entered,
       datetime(last_seen,'localtime') AS last_seen, closed, total_sightings
FROM sessions WHERE badge='GOLD-STAR';
-- Two rows:
--   id=N | GOLD-STAR | <time A> | <gate time> | closed=1 | sightings=3
--   id=M | GOLD-STAR | <time B> | <perimeter time> | closed=0 | sightings=2

SELECT session_id, COUNT(*) FROM detections WHERE badge='GOLD-STAR' GROUP BY session_id;
-- session_id=N: 3 detections (Person A)
-- session_id=M: 2 detections (Person B)
```

✅ **What this proves:** the system treats each presence-from-entry-to-exit as a separate visit. Same badge assigned to different people produces independent visits. Detections are correctly attributed to the visit they happened during via `session_id`.

---

## TEST 15 — Jetson heartbeats

Topbar shows `0 / 4 JETSONS ONLINE`.

### Linux
```bash
mkdir -p data/heartbeats
touch data/heartbeats/hb_central.txt
touch data/heartbeats/hb_edge-1.txt
touch data/heartbeats/hb_edge-2.txt
```

### Windows
```powershell
New-Item -ItemType Directory -Force -Path "data\heartbeats" | Out-Null
New-Item -ItemType File -Path "data\heartbeats\hb_central.txt"
New-Item -ItemType File -Path "data\heartbeats\hb_edge-1.txt"
New-Item -ItemType File -Path "data\heartbeats\hb_edge-2.txt"
```

**Browser** (refresh or wait 5s):
- Topbar: `0 / 4` → `3 / 4 JETSONS ONLINE` ✓

**Wait 90+ seconds. Refresh:**
- Topbar: `3 / 4` → `0 / 4` (files older than `TRINETRA_HB_STALE=90`) ✓

✅ Heartbeat freshness drives count.

---

## TEST 16 — Change loitering threshold via `.env`

**Stop server** (Ctrl+C in Terminal 1).

If `.env` doesn't exist:
```bash
cp .env.example .env       # Linux
Copy-Item .env.example .env  # Windows
```

Edit `.env`:
```ini
TRINETRA_LOITER_MINUTES=2
```

Save. Restart server:
```bash
./venv/bin/python3 app.py    # Linux
python app.py                # Windows
```

**Startup logs:**
```
[config] LOITER_MIN    = 2 min
```

Drop a current detection:

### Linux
```bash
touch "data/incoming/cam-parking_ORANGE-DIAMOND_$(date +%Y%m%d-%H%M%S).jpg"
```

### Windows
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-parking_ORANGE-DIAMOND_$ts.jpg"
```

**Wait ~3 minutes.** Within 30s after the 2-minute mark:
- Red loitering banner appears for ORANGE-DIAMOND ✓

✅ Loitering threshold is configurable via `.env`. No SQL needed.

Set back to `TRINETRA_LOITER_MINUTES=15` when done (or remove the line).

---

## TEST 17 — Reset everything

### Linux
```bash
# Ctrl+C the server
rm -rf data/
cd backend && ./venv/bin/python3 app.py
```

### Windows
```powershell
# Ctrl+C the server
Remove-Item -Recurse -Force data
cd backend && python app.py
```

Server starts fresh — DB recreated, no detections. Topbar back to 0/0/0.

✅ Clean restart.

---

# ✅ Final checklist

| # | What you verified |
|---|---|
| 1 | File watcher picks up new files |
| 2 | Click premises row → modal with image + log history |
| 3 | Session extends + click log entry swaps image |
| 4 | New badges create new sessions |
| 5 | Bad filenames go to dead-letter |
| 6 | DB schema correct (no `config` table, `session_id` column exists) |
| 7 | Exit gate from `.env` closes session |
| 8 | Loitering alert fires after threshold |
| 9 | Ack mutes alert |
| 10 | Whitelist dropdown from `classes.txt` |
| 11 | classes.txt read live (no restart) |
| 12 | Unknown badges tagged `(auto)` |
| 13 | Filters + search + CSV export |
| 14 | Click any log/notification → image modal |
| **14.5** | **Badge hand-off creates separate visits (Visits tab)** |
| 15 | Heartbeat freshness drives jetson count |
| 16 | Loitering threshold via `.env` + restart |
| 17 | Reset everything works cleanly |

If all 18 pass → ready to deploy.

---

# 🐛 Troubleshooting

| Symptom | Fix |
|---|---|
| "permission denied" on DB | `sudo chown -R $USER:$USER data/` |
| "address already in use" | Set `TRINETRA_PORT=8080` in `.env`, restart |
| Browser shows old data | Hard refresh: Ctrl+Shift+R |
| File dropped, dashboard doesn't update | Check Terminal 1 for `bad filename → dead-letter` |
| Duration shows huge numbers (300+ min) | Old data from before timezone fix. `rm -rf data/`, restart |
| Logs page empty | Same as above — `rm -rf data/`, restart |
| `.env` path doesn't work | Use forward slashes on Windows too: `C:/Users/...` |
| Whitelist dropdown empty | Check `backend/classes.txt` has uncommented lines |

---

# 📁 Folder structure

```
trinetra-monitor-full/
├── .env.example              ← template
├── .env                      ← YOUR config (optional, copy from .env.example)
├── RUN.md                    ← short setup
├── TESTING.md                ← this file
├── STEP-BY-STEP.md           ← deep-dive
├── backend/
│   ├── app.py                ← FastAPI server
│   ├── config.py             ← reads .env, normalizes paths
│   ├── schema.sql            ← 4 SQLite tables
│   ├── classes.txt           ← ★ EDIT: YOLO badge names
│   ├── requirements.txt
│   └── venv/                 ← created during install
├── frontend/
│   ├── index.html
│   ├── logs.html
│   ├── visits.html             ← NEW: visits page
│   ├── whitelist.html
│   ├── css/style.css
│   └── js/
│       ├── api.js
│       ├── common.js
│       ├── dummy-data.js
│       ├── live.js
│       ├── logs.js
│       ├── visits.js           ← NEW: visits page logic
│       └── whitelist.js
└── data/                     ← auto-created on first run
    ├── trinetra.db
    ├── incoming/             ← drop .jpg files here
    ├── archive/
    ├── heartbeats/
    └── dead-letter/
```
