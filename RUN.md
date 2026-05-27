# Trinetra Monitor — Quick Setup

> 📖 **For full testing walkthrough → see [TESTING.md](TESTING.md)** (17 tests covering every feature)

## 🪟 Windows

```powershell
# Extract trinetra-monitor-full.zip via File Explorer

cd trinetra-monitor-full

# Copy environment template
Copy-Item .env.example .env
# Edit .env in any text editor if you want to change paths/port

cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

If `Activate.ps1` errors with "running scripts is disabled":
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

## 🐧 Linux / Jetson

```bash
tar -xzf trinetra-monitor-full.tar.gz
cd trinetra-monitor-full

# Copy environment template
cp .env.example .env
# Edit .env if you want to change paths/port

cd backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python3 app.py
```

## Open dashboard

http://localhost:8000

## Verify it works

Drop a test detection:

**Linux:**
```bash
cd ..  # back to project root
touch "data/incoming/cam-entrance_RED-CIRCLE_$(date +%Y%m%d-%H%M%S).jpg"
```

**Windows:**
```powershell
cd ..
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType File -Path "data\incoming\cam-entrance_RED-CIRCLE_$ts.jpg"
```

Within 5 seconds, RED-CIRCLE appears in the dashboard's "Currently in Premises" table.

## The `.env` file — cross-platform paths

The `.env` file at the project root controls all paths and settings. Copy from `.env.example`:

```ini
# Paths
TRINETRA_DATA=./data            # default — relative, works on both OS
# TRINETRA_DATA=/home/jetson/trinetra-data       # Linux absolute
# TRINETRA_DATA=C:/Users/Pratik/trinetra-data    # Windows (use forward slashes!)

# Server
TRINETRA_PORT=8000
TRINETRA_JETSONS=central,edge-1,edge-2,edge-3

# Detection / loitering
TRINETRA_LOITER_MINUTES=15
TRINETRA_SESSION_GAP_MINUTES=5
TRINETRA_EXIT_CAMERAS=cam-gate
```

Change any value → restart the server → behavior updates. Code never needs editing.

## Managing badge classes — `backend/classes.txt`

Plain text file, one badge per line. Drives the whitelist dropdown and logs filter.

```
RED-CIRCLE
BLUE-SQUARE
GREEN-TRIANGLE
...
```

Match this to your YOLO model's `data.yaml` `names:` list. Edit the file, refresh the browser — no restart needed.

## Folder structure

```
trinetra-monitor-full/
├── .env                  ← your config (copied from .env.example)
├── .env.example          ← template
├── backend/
│   ├── app.py            ← FastAPI server
│   ├── config.py         ← reads .env, normalizes paths
│   ├── classes.txt       ← ★ EDIT: badge names
│   ├── schema.sql
│   └── requirements.txt
├── frontend/             ← static HTML/CSS/JS dashboard
└── data/                 ← created on first run (path from .env)
    ├── trinetra.db
    ├── incoming/         ← drop .jpg files here
    ├── archive/          ← processed images
    └── heartbeats/       ← jetson liveness files
```

See **TESTING.md** for the complete 17-test walkthrough.
