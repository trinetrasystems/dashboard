# Trinetra Perimeter Dashboard — Running the Server

All commands run from the project root:

```bash
cd /home/yash/Trinetra/dashboard
```

## Start (detached — keeps running after you close the terminal)

```bash
setsid nohup ./backend/venv/bin/python3 -u backend/app.py >> trinetra.log 2>&1 < /dev/null &
```

`setsid` is what detaches it. Plain `&` or `nohup` alone leaves the process tied
to your terminal session, and it stops when that session ends.

`-u` makes the log unbuffered. Without it, alert and ingest messages sit in
Python's output buffer and `tail -f trinetra.log` shows nothing while alerts fire.

## Start (foreground — logs to the screen, Ctrl+C to stop)

```bash
cd /home/yash/Trinetra/dashboard/backend
./venv/bin/python3 -u app.py
```

## Check it is running

```bash
pgrep -af "app.py"
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/
```

`200` means the dashboard is up.

## Stop

```bash
pkill -f "venv/bin/python3.*app.py"
```

## Restart

```bash
pkill -f "venv/bin/python3.*app.py"
sleep 2
setsid nohup ./backend/venv/bin/python3 -u backend/app.py >> trinetra.log 2>&1 < /dev/null &
```

Restart after editing `.env` or any file in `backend/`. Frontend files
(`frontend/`) are picked up on a normal page reload — no restart needed.

## Watch the log

```bash
tail -f /home/yash/Trinetra/dashboard/trinetra.log
```

## Open the dashboard

http://localhost:8000
