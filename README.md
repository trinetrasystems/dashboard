# Trinetra Perimeter dashboard

Perimeter-violation monitoring for one site, fully offline: a DeepStream 7.1 pipeline on the Jetson watches the
cameras, a FastAPI dashboard turns what it sees into **client-configured alerts** that flash and beep on every open
dashboard page. No internet is needed; Telegram / email are optional extras.

Built the same way as Trinetra Workplace Analytics (same sign-in, Activity Logs, optimisations and design). Installed
and upgraded on a client's Jetson with the `trinetra-perimeter` skill (Docker container, port 8002, Tailscale Funnel
`:8443`).

## How it works

```
cameras ─▶ DeepStream (nvurisrcbin → nvstreammux → nvinfer YOLO → nvtracker → probe)        backend/pipeline/
             │  every object: kind (person / object), the regions it is in, outside the line?, track id
             ▼
           data/incoming/<camera>_<class>_<YYYYmmdd-HHMMSS>-<ms>.jpg  +  same name .json (the frame description)
             ▼
dashboard: archive the image ─▶ Detection Log / Visits                                    backend/app.py
           frame description ─▶ alert rule engine ─▶ alerts table ─▶ flashing strip + beep  backend/alert_rules.py
```

* **Regions** (Alerts → Regions, or DeepStream → Zones), drawn on each camera's picture:
  the perimeter **line** with its outside side, named **alert** regions (wall top, concertina), named **watch**
  regions, and **ignore** regions (trees, roads: never reported).
* **Image classes** (backend/classes.txt): `Boundry-object` (in an alert region), `outside-person`,
  `outside-object`, `zone-person`, `zone-object` (in a watch region).
* **Alert rules** (Alerts → Alert rules), set by the client: *presence* (alert at once), *dwell* (present for N
  minutes), *count* (N or more at once), *camera offline* (N minutes). Each rule: who (person / object / any),
  where (alert regions, outside the line, any region, or one named region), cameras, days and hours, severity,
  sound, repeat while still present, cooldown, and the **presence gap** (how long an object may be missed and still
  be the same presence).
* **Alerts** (Alerts → Alerts): history with filters, pager, snapshot, Ack / Ack all. Every page shows the red
  flashing strip and beeps while an alert is open (mute per browser).

### Edge cases the rule engine handles

* An object missed for a while (tracked 1–10 s, missed 11–20 s, tracked 21–25 s) is **one** presence as long as each
  break is shorter than the rule's gap (default 60 s); the dwell time runs from the first sighting.
* The presence is found in both directions around each frame, so frames that arrive late or out of order never
  raise a second alert for the same presence; a backlog after a restart is replayed in time order.
* A tracker id switch, a person crossing between cameras' rules, border jitter between two regions: the presence is
  per rule and region, not per track id.
* The pipeline saves an image at once for every new object (probation age 0) and for every new crowd peak; a
  single-frame object right after a save is held and written a moment later, never dropped; held frames are written
  when the pipeline stops; file names carry milliseconds.
* Camera offline alerts wait for the camera to stay back for 60 s before a new outage can alert (a flapping camera
  gives one alert, not dozens).

## Pages

| Page | Who | What |
|---|---|---|
| Live | users with *live* | today's KPIs, open alerts banner, zone cards, today's detections, alert log |
| Alerts | *live* (rules: *alert rules*) | alert history, alert rules, regions per camera |
| Detection Log | *logs* | every saved image, filters, charts, export |
| Visits | *logs* | each presence of a class at a camera |
| Whitelist | *manage alerts* | classes / detector labels that never fire a rule |
| Users / Admin Accounts | admins / Trinetra team | accounts and permissions |
| Activity Logs, Settings, DeepStream | Trinetra team | audit trail; settings; cameras, model, engine, pipeline |

## Run it on a laptop (development)

See RUN.md. Tests: TESTING.md.
