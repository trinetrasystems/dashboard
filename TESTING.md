# Testing the perimeter dashboard

All test scripts live with the install skill (`~/.claude/skills/trinetra-perimeter/reference/`) and the VAPT report
folder; they run against a laptop test server or the Jetson. Browser tests use Playwright + Google Chrome.

| Test | What it proves | Last result (25 Nov 2026) |
|---|---|---|
| `test_alert_rules.py` | the rule engine: presence, dwell, count, schedule (overnight, days), whitelist (label + class), camera filter, cooldown, repeat, offline (flapping camera), migration, validation; the intermittent-tracking cases (tracked 1–10 s, lost 11–20 s, tracked 21–25 s = one presence), id switches, out-of-order frames, midnight, exact threshold, a single frame; **throw over the wall** (28 Nov 2026): a thrown object → "Throw over the wall" (warning, kind throw), never a critical breach alone, 1 min cooldown (no flood), an existing install gets the rule once, a deleted one stays deleted | 47/47 |
| `test_presence_fuzz.py` | randomised: thousands of frames with random breaks, other kinds / regions, duplicates, random / shuffled / reversed arrival — the presence found for every frame equals a brute-force answer, and no presence is ever without an alert | 144/144 (72 scenarios) |
| `test_savepolicy.py` | which frames the pipeline saves: a new object or a crowd peak at once, a single-frame object never lost (held), no flood | 10/10 |
| `e2e.js` | sign-in for every role, accounts, Activity Logs, Settings, an image dropped → Live, own password, gzip / 304 | 36/36 |
| `e2e_alerts.js` | regions drawn in the browser, rules made / edited / switched / deleted, real image + frame-description files → breach, intermittent dwell, count, whitelist; flashing strip + beep + blinking title on another page; Ack / Ack all; Live-only user limits; the pager on every page | 48/48 |
| `spk_e2e.js` | Speaker tab: settings, permission, jobs reach the host agent, a new alert → a beep job | 15/15 |
| `xss_e2e.js` | an alert rule / detector label with HTML is shown as text on every page | 8/8 |
| `throw_e2e.py` | throw / landing groundwork (27 Nov 2026): zones (near wall, throw band, landing zone) + every refusal, the client's editor cannot touch them, class filter in nvinfer, throw camera at every frame, day / night, normal frames (whole 1920x1080, skipped while something is in the landing zone), staged-test log → ground_truth.csv, packages with meta + video, the laptop receiver | 45/45 |
| `throw_ui.js` | the same in the browser: new zone tools, a band drawn with the mouse, the drawing layer exactly over the picture, camera / settings / Recordings, the client's locked regions | 17/17 |
| `fa_e2e.py` | "False alarm" on an alert (27 Nov 2026): client admin + team may, a user with Acknowledge alerts may not (403); acknowledged by it, note + who kept, twice → 409, camera offline → 400; feedback.jsonl line per mark / undo with what the detection learns (landing → bank, throw → negatives, person → none); filter + count; undo; Activity Logs | 29/29 |
| `fa_ui.js` | the same in the browser: the button (not on camera offline), the dialog saying what is learned, the row after marking, the False alarms filter + count, Undo, the guard sees no button | 17/17 |
| `backend/pipeline/patchcore.py` (self-test; also in the install check) | PatchCore's base (27 Nov 2026): the fixed tile grid, the wide_resnet50_2 backbone offline on the GPU (FP16), [2, 1536, 36, 36] features, the batched FP16 nearest-patch search equals the exact FP32 distance (0.03 % where it matters) | PASS on J-Hyderabad |
| `backend/pipeline/throw.py` (self-test; also in the install check) | the throw path test (lob, straight, noisy: yes; zig-zag, drift, along the top, inside → outside, against gravity, too short: no) and the detector on pictures (one throw → exactly 1, noise / a light flash → 0, a person's motion ignored) | PASS |
| `zones.py` (self-test) | + the crossing: outside 3x then inside 3x = once; wobbling on the line, leaving, inside first = never | PASS |
| `clip_check.py` + `steady_clip.py` + `clip_demo_video.py` | real throw clips through the DEPLOYED pipeline (each an RTSP camera on a test install): the user's 3 handheld throws, steadied (direct registration onto the first frame, drift ~1 px): 3/3 found, 1 alert each in the Alerts tab ("Throw over the wall", warning, the Jetson's review picture), 0 extra events, the same 3 runs out of 3; the demo videos draw only the Jetson's own track points | 28 Nov 2026 |
| `reconnect_stress.py` | cameras whose streams end every few seconds, several in the same watch tick, a frozen camera (connection open, no video, 120 s), a hang-up: 3 cameras at 848x464 (the muxer scales them) 15 min ≈ 14 reconnects each, video back every time, no crash / hang, RAM flat; 1 camera; frozen + hang-up: both back; **3 slow cameras** (4 s to answer) reconnecting together; final build: 1 pipeline start, 0 errors, 0 watchdog exits, Stop 2–4 s | 28 Nov 2026 |
| `realcam_soak.py` | continuous RTSP cameras (one piece repeated into one long file, no end): 0 reconnects allowed; fps, online time, throws, CROSSED, GPU / CPU / RAM, Stop time; 1080p H.264, 2560x1440 H.264 25 fps, 720p H.265 and a throw camera: 30 min site-like throw camera 30 fps, 100 % online, 0 reconnects; 4 cameras 20 min, 100 % online, 0 reconnects, throw camera 25 fps | 28 Nov 2026 |
| `b_e2e.py` | step B on a test install, live pipeline, RTSP at 30 fps: day + night test videos (16 throws + 26 not-throws each) scored, crossing on real people, `*near` / `*crossed` rules, camera reconnect: day 16/16, night 16/16, 0 false alarms, 30 fps, 0 dropped | 28 Nov 2026 |
| `throw_replay.py` + `eval_throws.py` | the same detector on files: six site-like test videos (3 seeds × day / night, 16 throws + 26 not-throws each) 91–92 of 96 found, **0 false alarms**; an unseen seed 16/16; margin: a fit limit of 7 px still raises 0 false alarms (5.5 px used) | 28 Nov 2026 |
| `budget_a4.py` | does it fit (plan step A4): 1 RTSP camera at 30 fps, YOLO26m + PatchCore 1/s (16 tiles, 2 % banks) + motion on every frame — detector on every 2nd frame: GPU ≈ 50 %, 30 fps, PatchCore 409 ms, motion 7 ms/frame (CPU 18 %), RAM 3.5 / 7.6 GB; every frame: GPU 90 %+. 2 cameras: 20 fps (too much for throw detection) | see SKILL.md |
| `vapt_v2.py` | every route × anonymous / live-only user / admin, traversal, SQL injection, CSRF, headers, sessions, body abuse, validation, hostile incoming files, lockout, errors, WebSocket, disclosure | 24/24 |
| worst case (`bench.py`, `ingest_bench.py`) | 1M detections, 100k alerts, 345k observations: every polled API fast, 10 Live pages at once, 800 frames burst with 30 rules, memory | see the report |

A frame description (what the pipeline writes next to each image, `<camera>_<class>_<YYYYmmdd-HHMMSS>-<ms>.json`):

```json
{"v": 1, "frame": "north-wall|2026-11-25T10:00:00.120|42", "camera": "north-wall", "ts": "2026-11-25T10:00:00.120",
 "objects": [{"label": "person", "kind": "person", "regions": ["Gate A"], "alert": false, "outside": false,
              "track": 7, "conf": 0.81, "box": [x, y, w, h]}]}
```

Write it first, then the `.jpg` (via a temporary name + rename), into `data/incoming/`.
