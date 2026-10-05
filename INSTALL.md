# Installing AgriSense

## Requirements

- Python 3.12+ (3.14 works; `requirements.txt` targets that)
- No system libraries beyond Python — no OpenCV, no MQTT broker, no build tools
- A browser for the dashboard

## Setup

```bash
git clone <repo-url> && cd agrisense
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
./run.sh              # production-ish server on :8000
# or, with your venv active, directly:
cd backend
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

- Dashboard: <http://localhost:8000/>
- Simulation console: <http://localhost:8000/simulation>

## Common flags

| Flag                       | Meaning                                        |
| -------------------------- | ---------------------------------------------- |
| `./run.sh 8080`            | Custom port                                    |
| `HOST=127.0.0.1 ./run.sh`  | Localhost-only bind                            |
| `SPEED=300 ./run.sh`       | Faster simulated clock                         |
| `RELOAD=1 ./run.sh`        | Auto-reload on code changes (dev)              |
| `AGRISENSE_WRITE_TELEMETRY=1 ./run.sh` | Persist telemetry to SQLite   |

## Troubleshooting

- **`ModuleNotFoundError: fastapi`** — the venv isn't active, or
  `run.sh` couldn't install into the current interpreter. Re-run
  `pip install -r requirements.txt`.
- **Port already in use** — pick another: `./run.sh 8081`.
- **Blank camera tiles** — feeds are MJPEG over `/api/cameras/{id}/stream.mjpg`;
  a strict browser extension can block it. Try another browser.
- **No data movement** — the simulation speed is controlled from the console
  page (top-right clock panel). Preset buttons set 1x..1800x.
