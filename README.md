# Trinetra Systems Monitor

Trinetra Systems Monitor is a smart loitering detection and personnel tracking system. It processes incoming detections from edge cameras (Jetson devices), groups them into logic "sessions" (visits), monitors for loitering, and provides a real-time monitoring dashboard and admin panel.

## Core Data Flow

The system operates on a continuous event-driven loop:

1. **Edge Detections (Ingest)**: Edge Jetson devices detect a person and save a `.jpg` image to the `data/incoming/` directory. The filename contains metadata (e.g., `cam-parking_RED-CIRCLE_20260528-193935.jpg`).
2. **Watchdog Processing**: The backend (`app.py`) watches the `incoming` directory using a file watcher. As soon as a file arrives:
   - The image is parsed to extract the **Camera**, **Badge** (person ID), and **Timestamp**.
   - A raw **Detection** log is inserted into the database.
   - The image is moved to the `data/archive/` folder for long-term storage.
3. **Session Management (Visits)**: The system groups rapid, successive detections of the same badge into a continuous **Session**. If a person is seen, their session opens. If they aren't seen for a certain gap (e.g., 5 mins) or they pass an "Exit Camera" (e.g., `cam-gate`), their session closes.
4. **Loitering Check**: A background loop continuously checks open sessions. If a session stays open longer than the configured threshold (e.g., 15 mins), a loitering **Alert** is fired, and a red banner appears on the dashboard.
5. **Dashboard & Admin**: The frontend uses a WebSocket to receive live updates. Users can acknowledge alerts (which auto-whitelists the badge) or manage the settings and database directly via the Admin panel.
6. **Visit Recordings**: If a video file for a visit exists on local storage, the Visits tab plays it inline or offers it for download. See [Visit Recordings](#visit-recordings-video-playback) for the required naming and file format.

---

## Database Tables Explained

The entire system state is stored in a single SQLite database (`data/trinetra.db`), managed automatically by the backend. It consists of four main tables:

### 1. `detections` Table
**What it is:** The raw, unfiltered log of every single time a camera spotted a person.
- **Purpose**: Provides a granular, second-by-second history of movement.
- **Key Fields**:
  - `filename`: Links the detection to its saved image in `data/archive/`.
  - `camera`: Which camera saw them.
  - `badge`: The identity of the person (e.g., RED-CIRCLE).
  - `type`: Either `entry` (first time seen), `sighting` (seen again), or `exit` (seen at an exit gate).
- **Cleanup**: If you delete a row from this table via the Admin panel, the system also automatically deletes the linked `.jpg` image from the hard drive to free up space.

### 2. `sessions` Table
**What it is:** The grouped "visits" of people currently or previously in the premises.
- **Purpose**: Instead of showing 50 separate detections for a person standing in front of a camera for 2 minutes, this table consolidates them into a single ongoing "Session".
- **Key Fields**:
  - `first_seen` & `last_seen`: Tracks exactly how long the person has been inside.
  - `total_sightings`: How many raw detections are grouped into this session.
  - `closed`: A boolean (`0` or `1`). If `0`, the person is currently in the building. If `1`, they have left or their session timed out.
- **Dashboard Link**: This table directly powers the "Currently in Premises" section on the Live Dashboard.

### 3. `alerts` Table
**What it is:** A log of loitering violations.
- **Purpose**: When a session exceeds the loitering threshold (e.g., 15 minutes), a row is added here.
- **Key Fields**:
  - `triggered_at`: When the loitering threshold was breached.
  - `acknowledged`: Whether a security operator clicked "Ack" on the dashboard.
  - `acknowledged_by`: Who acknowledged it (usually "operator").
- **Dashboard Link**: This table powers the red "LOITERING DETECTED" banner. Once an alert is acknowledged, the banner is dismissed for that specific badge.

### 4. `whitelist` Table
**What it is:** A list of badges that are exempt from loitering alerts.
- **Purpose**: Used for staff members, guards, or delivery personnel who are allowed to stay in the premises indefinitely without triggering alarms.
- **Key Fields**:
  - `badge`: The ID of the exempt person.
  - `reason`: Why they were whitelisted (e.g., "Auto-whitelisted: acked alert #12").
- **Workflow**: When an operator clicks "✓ Ack" on a loitering alert, the badge is automatically added to this table. The operator can later remove them from the whitelist using the "✕ Remove WL" button or the Whitelist tab.

---

## Background Automated Tasks

To keep the system running efficiently indefinitely, two background loops run continuously:

1. **Loitering Loop (`loitering_loop`)**: 
   - Runs every 10 seconds.
   - Checks if any open session has been active longer than `TRINETRA_LOITER_MINUTES`.
   - If yes, inserts an alert into the `alerts` table and notifies the frontend via WebSocket.
   - Auto-closes sessions if no new detections have occurred within the `TRINETRA_SESSION_AUTO_CLOSE_MINUTES` timeframe.

2. **Image Cleanup Loop (`image_cleanup_loop`)**:
   - Runs every 24 hours (and once 60 seconds after server startup).
   - Scans the `data/archive/` folder for `.jpg` files older than `TRINETRA_IMAGE_RETENTION_DAYS`.
   - Deletes old images to prevent disk space exhaustion.
   - **Important**: It does *not* delete the database logs. The history remains forever, but the dashboard will simply show "NO IMAGE AVAILABLE" when viewing older logs.

## Admin Settings

Configuration can be updated on the fly without touching code via the **⚙ Admin Panel**. Settings are saved directly to the `.env` file and hot-reloaded into memory instantly. 

Settings include thresholds, retention policies, the visit-recording directory, Telegram alert integrations, and SMTP configurations for email notifications.

---

## Visit Recordings (Video Playback)

Every row on the **Visits** tab can play a recording of that visit. When a matching video is found, the row's **Video** column shows **▶ Watch** (plays inline in a modal, without leaving the page) and **⬇** (downloads the original file). Rows with no recording show `—`.

Trinetra treats the video directory as **read-only** — it never writes, renames, moves, or deletes anything there. The image-cleanup loop does not touch it either, so retention of recordings is entirely up to you.

### 1. Where the files live

Set the directory in `.env`:

```bash
TRINETRA_VIDEO_DIR=/mnt/nvr/recordings
```

Or edit it under **🎬 Visit Recordings** in the Admin Panel, which applies immediately with no restart. It can be any path readable by the server — an external drive, a NAS mount, or an NVR export folder. Relative paths resolve against the project root. If unset, it defaults to `./data/videos`.

The directory is re-read on every dashboard refresh, so dropping a new file in makes it appear within ~10 seconds. No restart, no re-indexing.

### 2. How files are named

The filename maps a video to a visit:

```
<badge>_<date>.mp4
```

`<date>` is the visit's **local start date** — exactly the value shown in the Visits table's Date column. For a visit by badge `CAR-GJ18BR9090` on 05 Aug 2026:

```
CAR-GJ18BR9090_20260805.mp4
```

Accepted variations:

| Variation | Example | Matches? |
|---|---|---|
| Compact date | `RED-CIRCLE_20260807.mp4` | ✅ |
| Dashed date | `RED-CIRCLE_2026-08-07.mp4` | ✅ |
| Any letter case | `red-circle_20260807.MP4` | ✅ |
| Extra suffix | `RED-CIRCLE_20260807_cam3.mp4` | ✅ |
| Wrong date for that badge | `RED-CIRCLE_20260101.mp4` | ❌ |

> **Visits that cross midnight** are matched on the date they **started**. A visit beginning 23:40 on 28 Jul and ending 01:15 on 29 Jul is named with `20260728`. Because timestamps are stored in UTC and converted to local time for matching, always name the file after what the dashboard displays — not the raw database value.

To list the exact filename every visit is looking for:

```bash
sqlite3 -header -column data/trinetra.db "
SELECT id AS visit, badge, date(first_seen,'localtime') AS visit_date,
       badge || '_' || strftime('%Y%m%d', first_seen, 'localtime') || '.mp4' AS expected_filename
FROM sessions ORDER BY first_seen DESC;"
```

### 3. Required file format

This is the most common reason a video is found but won't play. Browsers are strict about **both** the container and the codec inside it.

| Container | Video codec | Audio codec | Result |
|---|---|---|---|
| `.mp4` / `.m4v` | **H.264** | AAC | ✅ Plays everywhere — **recommended** |
| `.webm` | VP8 / VP9 | Opus / Vorbis | ✅ Plays in all modern browsers |
| `.mp4` | H.265 / HEVC | — | ⚠️ Safari only; not Chrome or Firefox |
| `.mkv` / `.avi` / `.mov` | any | — | ❌ No browser will play it |

**H.264 video + AAC audio in an `.mp4`** is the safe choice, and it is what Jetson hardware encoders produce natively.

Files in a detected-but-unplayable container (`.mkv`, `.avi`, `.mov`) still appear in the table — the row shows **⚠ no preview** with the download link intact, so you can tell "wrong format" apart from "wrong filename".

Check what you have:

```bash
ffprobe -v error -show_entries stream=codec_name,codec_type -of csv=p=0 yourvideo.mp4
```

Convert anything else:

```bash
ffmpeg -i input.mkv -c:v libx264 -c:a aac -movflags +faststart output.mp4
```

> `-movflags +faststart` moves the MP4 index to the front of the file. Without it the browser must download the **entire** file before showing the first frame. Always include it for anything served over HTTP.

### 4. Playback and seeking

Recordings are served from `/video/<filename>` with full **HTTP Range** support, which means:

- Seeking works instantly — dragging the scrubber jumps without re-downloading
- Safari and iOS can play the files (they refuse video served without range support)
- Large recordings start streaming immediately instead of buffering whole

Only filenames inside the configured directory are served; path traversal and non-video extensions are rejected.

### 5. Troubleshooting

| Symptom | Cause |
|---|---|
| Column shows `—` | No file matched. Compare `ls $TRINETRA_VIDEO_DIR` against the expected-filename query above. |
| Shows **⚠ no preview** | File found, but the container isn't browser-playable. Convert it (see §3). |
| Plays but won't seek | Not possible via this endpoint — if it happens, something else is proxying `/video/`. |
| Nothing appears at all | Confirm the resolved directory with `curl localhost:8000/api/visits/<id>/video` — it echoes back the `video_dir` actually in use, which the Admin Panel may have overridden. |

---

## How to Run the System

### 1. First Time Setup & Run
If this is your first time setting up the dashboard on a new machine, you need to create a virtual environment and install the required dependencies before running it:

```bash
cd /home/yash/dashboard

# Create a virtual environment named "venv"
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install the required Python packages
pip install -r backend/requirements.txt

# Run the server
python backend/app.py
```

### 2. Subsequent Runs (Every time after the first)
Once the virtual environment is already created and dependencies are installed, you just need to activate it and run the script:

```bash
cd /home/yash/dashboard

# Activate the existing virtual environment
source venv/bin/activate

# Run the server
python backend/app.py
```

> **Note**: For production environments, it is highly recommended to set this command up as a `systemd` service so that the dashboard starts automatically if the server reboots.
