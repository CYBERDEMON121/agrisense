# AgriSense

A self-contained farming IoT platform: a live sensor/actuator simulation
engine with a **monitoring dashboard** and a **simulation console**, served by a
single FastAPI process over REST + WebSocket.

## What it simulates

- **Soil sensors** — per-plot probe fleet (moisture, temperature, EC) with
  per-device health, failures and history persistence.
- **Weather station** — temperature, humidity, wind, rain, irradiance driven by
  a diurnal model you can override from the console.
- **Cameras** — per-area RGB feeds of the land and canopy, streamed as MJPEG,
  including a camera attached to the airborne drone.
- **Irrigation** — pump, 600 m³ tank, borewell fallback, zone solenoids with
  ramp-up, shared-pump head model and per-plot application uniformity.
- **Spray drone** — boustrophedon coverage planner with a physical deposition
  model (wind-widened effective swath, drift-adjusted nozzle flow, battery and
  payload budgets, sorties, wind aborts, dose-response efficacy).
- **Automation** — closed loop from soil moisture → zone valves, pest/disease
  pressure → drone spray missions, with a water-spray interlock, decision log
  and alert stream.

## Pages

| Route        | Page                                             |
| ------------ | ------------------------------------------------ |
| `/`          | Dashboard — live monitoring of every device      |
| `/dashboard` | Same as above                                    |
| `/simulation`| Simulation console — clock, fields, scenarios    |

The dashboard is read-only: thresholds, scenarios and manual overrides live on
the simulation page. Events flow over one shared WebSocket with a REST
fallback.

## Install & run

See [INSTALL.md](INSTALL.md) for a full guide. Short version:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh
# open http://localhost:8000/
```

Useful knobs:

```bash
PORT=8080 RELOAD=1 SPEED=300 ./run.sh
```

Environment overrides (`AGRISENSE_*`):

| Variable                     | Default   | Purpose                              |
| ---------------------------- | --------- | ------------------------------------ |
| `AGRISENSE_HOST`             | `0.0.0.0` | Bind address                         |
| `AGRISENSE_PORT`             | `8000`    | Port                                 |
| `SPEED`                      | `60`      | Simulated seconds per wall second    |
| `AGRISENSE_DB`               | `backend/data/agrisense.db` | SQLite telemetry DB   |
| `AGRISENSE_WRITE_TELEMETRY`  | `0`       | 1 = persist plots/drone telemetry    |

## Repository layout

```
requirements.txt      small dependency set (FastAPI, uvicorn, numpy, Pillow)
run.sh                one-command launcher
backend/
  app/
    main.py           FastAPI app, static + page routes
    config.py         Settings dataclass & AGRISENSE_* wiring
    bus.py            in-process event bus (topics like land/north-03/soil/moisture)
    clock.py          simulation clock, time helpers
    db.py             SQLite telemetry store
    api/routes.py     REST + WS command surface
    logic/automation.py  irrigation/spray/health rules & decision log
    sim/
      engine.py       SimulationEngine tying everything together
      field.py        FieldModel: plots, areas, heterogeneity, water balance
      weather.py      diurnal weather model with force/release
      devices/        sensors, irrigation, drone, cameras
    static/           dashboard.html, simulation.html, js/, css/
  tools/              fly_mission_sim.py (drone invariants), check_self_attrs.py
  tests/              (reserved)
INSTALL.md            installation guide
```

## Simulation model highlights

- **Field heterogeneity** — each area has its own soil texture, delivery
  uniformity, depletion, pest/disease pressure, GDD baseline; every plot
  deviates from it, so no two plots behave alike and the map is actually
  interesting.
- **Automation honours manual intent** — a zone opened by an operator runs to
  its target; the closed loop only re-closes zones it opened itself.
- **Drone spray honesty** — pass lines are spaced by their effective treated
  width, spray volume is metered to the ground actually swept, nozzle flow is
  set from the label rate inflated by modeled deposition losses, and efficacy
  follows a dose-response Hill curve. `tools/fly_mission_sim.py` asserts
  coverage/dose/volume/termination invariants per area.

## Verification

```bash
cd backend
python3 tools/fly_mission_sim.py --area north     # drone invariants
python3 tools/fly_mission_sim.py --area orchard
```

A jsdom-based page harness (both pages, ~141 checks) runs against a live
server when available.

## License

MIT
