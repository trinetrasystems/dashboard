# Testing the perimeter dashboard

All test scripts live with the install skill (`~/.claude/skills/trinetra-perimeter/reference/`) and the VAPT report
folder; they run against a laptop test server or the Jetson. Browser tests use Playwright + Google Chrome.

| Test | What it proves | Last result (25 Nov 2026) |
|---|---|---|
| `test_alert_rules.py` | the rule engine: presence, dwell, count, schedule (overnight, days), whitelist (label + class), camera filter, cooldown, repeat, offline (flapping camera), migration, validation; the intermittent-tracking cases (tracked 1–10 s, lost 11–20 s, tracked 21–25 s = one presence), id switches, out-of-order frames, midnight, exact threshold, a single frame | 39/39 |
| `test_presence_fuzz.py` | randomised: thousands of frames with random breaks, other kinds / regions, duplicates, random / shuffled / reversed arrival — the presence found for every frame equals a brute-force answer, and no presence is ever without an alert | 144/144 (72 scenarios) |
| `test_savepolicy.py` | which frames the pipeline saves: a new object or a crowd peak at once, a single-frame object never lost (held), no flood | 10/10 |
| `e2e.js` | sign-in for every role, accounts, Activity Logs, Settings, an image dropped → Live, own password, gzip / 304 | 36/36 |
| `e2e_alerts.js` | regions drawn in the browser, rules made / edited / switched / deleted, real image + frame-description files → breach, intermittent dwell, count, whitelist; flashing strip + beep + blinking title on another page; Ack / Ack all; Live-only user limits; the pager on every page | 48/48 |
| `spk_e2e.js` | Speaker tab: settings, permission, jobs reach the host agent, a new alert → a beep job | 15/15 |
| `xss_e2e.js` | an alert rule / detector label with HTML is shown as text on every page | 8/8 |
| `vapt_v2.py` | every route × anonymous / live-only user / admin, traversal, SQL injection, CSRF, headers, sessions, body abuse, validation, hostile incoming files, lockout, errors, WebSocket, disclosure | 24/24 |
| worst case (`bench.py`, `ingest_bench.py`) | 1M detections, 100k alerts, 345k observations: every polled API fast, 10 Live pages at once, 800 frames burst with 30 rules, memory | see the report |

A frame description (what the pipeline writes next to each image, `<camera>_<class>_<YYYYmmdd-HHMMSS>-<ms>.json`):

```json
{"v": 1, "frame": "north-wall|2026-11-25T10:00:00.120|42", "camera": "north-wall", "ts": "2026-11-25T10:00:00.120",
 "objects": [{"label": "person", "kind": "person", "regions": ["Gate A"], "alert": false, "outside": false,
              "track": 7, "conf": 0.81, "box": [x, y, w, h]}]}
```

Write it first, then the `.jpg` (via a temporary name + rename), into `data/incoming/`.
