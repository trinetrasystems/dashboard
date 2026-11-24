# Running the perimeter dashboard

## On a client's Jetson (production)

Installed and upgraded with the `trinetra-perimeter` skill: a Docker container (`trinetra-perimeter`), the data
folder on the host, systemd start at boot plus a watch timer, Tailscale Funnel on `:8443`.
On the Jetson:

```bash
sudo systemctl status perimeter-page          # boot service
sudo docker logs --tail 50 trinetra-perimeter # container
tail -f /home/nvidia/PERIMETER_data/logs/dashboard.log     # dashboard + pipeline log
```

The pipeline is started and stopped on the DeepStream page (Trinetra team); it restarts by itself after a crash
or a memory stop.

## On a laptop (development)

```bash
cd dashboard-jetson
python3 -m venv backend/venv && backend/venv/bin/pip install -r backend/requirements.txt
cp .env.example .env            # set SUPPORT_PASSWORD (Trinetra team sign-in)
backend/venv/bin/python3 -u backend/app.py
```

Open http://localhost:8000 and sign in as the Trinetra team (`SUPPORT_USERNAME` / `SUPPORT_PASSWORD`).
Without DeepStream the DeepStream page says so; drop test images (with a `.json` frame description, see
TESTING.md) into `data/incoming/` to exercise the alert rules.

Restart after editing `.env` or `backend/`; frontend files are picked up on a page reload.
