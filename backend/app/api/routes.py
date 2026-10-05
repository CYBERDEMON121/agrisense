"""REST + WebSocket + MJPEG API for the AgriSense dashboard.

Read endpoints serve the live engine state; control endpoints drive the sim
(clock speed, manual irrigation, manual missions) and drone commands.  The
WebSocket pushes a compact frame at the dashboard's own refresh rate so the UI
never has to poll.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..clock import to_local
from ..config import Settings
from ..sim.engine import SimulationEngine

router = APIRouter(prefix="/api", tags=["agrisense"])

#: How often the WebSocket pushes a full frame, in wall-clock seconds.
WS_PUSH_HZ = 2.0


def engine_of(request: Request) -> SimulationEngine:
    return request.app.state.engine


# --------------------------------------------------------------------------- #
# read endpoints
# --------------------------------------------------------------------------- #
@router.get("/state")
def get_state(request: Request, plots: bool = True, history: bool = True) -> dict[str, Any]:
    return engine_of(request).snapshot(include_plots=plots, include_history=history)


@router.get("/summary")
def get_summary(request: Request) -> dict[str, Any]:
    """Small, cheap payload for the KPI header (no plot list)."""
    snap = engine_of(request).snapshot(include_plots=False, include_history=False)
    keep = ("sim", "weather", "field", "automation", "drone", "irrigation")
    return {k: snap[k] for k in keep}


@router.get("/plots")
def get_plots(request: Request,
              area: str | None = Query(None, description="filter by area id")) -> dict[str, Any]:
    engine = engine_of(request)
    plots = engine.field.plots
    if area:
        plots = [p for p in plots if p.area_id == area]
    return {"count": len(plots), "plots": [p.state() for p in plots]}


@router.get("/plots/{plot_id}")
def get_plot(request: Request, plot_id: str) -> dict[str, Any]:
    engine = engine_of(request)
    plot = engine.field.by_id.get(plot_id)
    if plot is None:
        raise HTTPException(status_code=404, detail=f"unknown plot {plot_id}")
    probe = engine.sensors.probes.get(plot.sensor_id)
    return {
        "plot": plot.state(),
        "crop": {
            "key": plot.crop_key, "label": plot.crop.label, "t_base": plot.crop.t_base,
            "root_depth_m": plot.crop.root_depth_m, "kc": round(plot.kc, 3),
            "stage": plot.stage_name, "stage_index": plot.stage_index,
            "stages": [{"name": s.name, "gdd": s.gdd} for s in plot.crop.stages],
            "taw_mm": round(plot.taw_mm, 1), "depletion_mm": round(plot.depletion_mm, 1),
        },
        "sensor": probe.state() if probe else None,
        "camera": plot.camera_id,
        "irrigated_ts": plot.irrigated_ts,
        "irrigated_local": to_local(plot.irrigated_ts).isoformat() if plot.irrigated_ts else None,
        "last_spray_ts": plot.last_spray_ts,
        "last_spray_local": to_local(plot.last_spray_ts).isoformat() if plot.last_spray_ts else None,
    }


@router.get("/plots/{plot_id}/history")
def get_plot_history(request: Request, plot_id: str, hours: float = 24.0) -> dict[str, Any]:
    engine = engine_of(request)
    if plot_id not in engine.field.by_id:
        raise HTTPException(status_code=404, detail=f"unknown plot {plot_id}")
    rows = engine.plot_history(plot_id, hours)
    return {"plot_id": plot_id, "hours": hours, "points": len(rows), "series": rows}


@router.get("/areas")
def get_areas(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    out = []
    for area in engine.field.areas.values():
        plots = engine.field.plots_in_area(area.area_id)
        out.append({**area.state(),
                    "plots": len(plots), "area_ha": round(sum(p.area_ha for p in plots), 2),
                    "moisture": round(sum(p.moisture_pct for p in plots) / max(len(plots), 1), 1),
                    "pest": round(max((p.pest_pressure for p in plots), default=0), 3),
                    "health": round(sum(p.health for p in plots) / max(len(plots), 1), 3)})
    return {"areas": out, "bounds": engine.farm_bounds()}


@router.get("/devices")
def get_devices(request: Request, kind: str | None = None) -> dict[str, Any]:
    devices = engine_of(request).device_states()
    if kind:
        devices = [d for d in devices if d.get("kind") == kind]
    return {"count": len(devices), "devices": devices}


@router.get("/devices/{device_id}/history")
def get_device_history(request: Request, device_id: str, hours: float = 6.0,
                       limit: int = 600) -> dict[str, Any]:
    rows = engine_of(request).device_history(device_id, hours, limit)
    for row in rows:
        row["payload"] = _loads(row.get("payload"))
    return {"device_id": device_id, "points": len(rows), "series": rows}


@router.get("/weather")
def get_weather(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    w = engine.weather_state
    return {"weather": engine.weather_payload(w),
            "sunrise": round(engine.weather.sunrise, 2),
            "sunset": round(engine.weather.sunset, 2),
            "daylight_h": round(engine.weather.daylight_hours, 2)}


@router.get("/irrigation")
def get_irrigation(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    return {"irrigation": engine.irrigation.state(engine.clock.now())}


@router.get("/drone")
def get_drone(request: Request) -> dict[str, Any]:
    return {"drone": engine_of(request).drone.snapshot()}


@router.get("/missions")
def get_missions(request: Request, limit: int = 40) -> dict[str, Any]:
    return {"missions": engine_of(request).missions(limit)}


@router.get("/cameras")
def get_cameras(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    cams = []
    for cam in engine.cameras.cameras.values():
        cams.append({**cam.state(), "detection_list": [d.as_dict() for d in cam.last_dets]})
    return {"count": len(cams), "cameras": cams}


@router.get("/cameras/{camera_id}/detections")
def get_detections(request: Request, camera_id: str, label: str | None = None,
                   limit: int = 100) -> dict[str, Any]:
    cam = engine_of(request).cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"unknown camera {camera_id}")
    return {"camera": camera_id,
            "latest": [d.as_dict() for d in cam.last_dets],
            "history": engine_of(request).detections(limit, label)}


@router.get("/alerts")
def get_alerts(request: Request, limit: int = 100,
               unacked: bool = False) -> dict[str, Any]:
    return {"alerts": engine_of(request).alerts(limit, unacked)}


@router.get("/events")
def get_events(request: Request, limit: int = 60) -> dict[str, Any]:
    return {"events": engine_of(request).recent_events(limit)}


@router.get("/decisions")
def get_decisions(request: Request, limit: int = 60) -> dict[str, Any]:
    return {"decisions": engine_of(request).automation.decision_log(limit)}


@router.get("/bus/retained")
def get_retained(request: Request) -> dict[str, Any]:
    bus = engine_of(request).bus
    return {"messages": [{"topic": m.topic, "ts": m.ts, **m.payload}
                         for m in bus.retained()]}


@router.get("/bus/history")
def get_bus_history(request: Request, prefix: str | None = None,
                    limit: int = 100) -> dict[str, Any]:
    bus = engine_of(request).bus
    return {"messages": [{"topic": m.topic, "ts": m.ts, **m.payload}
                         for m in bus.history(prefix, limit)]}


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    return {"settings": engine.settings.as_dict(), "transport": engine.transport.info(),
            "topics": topic_reference()}


# --------------------------------------------------------------------------- #
# simulator console: driving the world, not just reading it
# --------------------------------------------------------------------------- #
@router.get("/sim/controls")
def sim_controls(request: Request) -> dict[str, Any]:
    """The simulator console's whole model: knobs, areas, faults, forcing."""
    return engine_of(request).controls()


@router.post("/sim/speed")
def set_speed(request: Request, speed: float = Query(..., ge=0.0, le=3600.0)) -> dict[str, Any]:
    return {"speed": engine_of(request).set_speed(speed)}


@router.post("/sim/pause")
def set_pause(request: Request, paused: bool = Query(True)) -> dict[str, Any]:
    return {"paused": engine_of(request).set_paused(paused)}


@router.post("/sim/step")
def sim_step(request: Request,
             seconds: float = Query(600.0, gt=0.0, le=21600.0)) -> dict[str, Any]:
    """Advance the world by hand - the only way to inspect one rule transition."""
    return engine_of(request).step_manual(seconds)


@router.post("/sim/thresholds")
def sim_thresholds(request: Request, payload: dict[str, float]) -> dict[str, Any]:
    """Retune the rule thresholds live."""
    engine = engine_of(request)
    try:
        applied = engine.apply_thresholds(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown threshold {exc.args[0]}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"thresholds": applied, "all": engine.controls()["tunables"]}


@router.post("/sim/weather")
def sim_weather(request: Request,
                rain_mm_h: float | None = Query(None, ge=0.0, le=120.0),
                wind_ms: float | None = Query(None, ge=0.0, le=30.0),
                temp_c: float | None = Query(None, ge=-20.0, le=60.0),
                cloud: float | None = Query(None, ge=0.0, le=100.0),
                hours: float = Query(4.0, gt=0.0, le=720.0)) -> dict[str, Any]:
    """Pin weather fields so the interlocks can be exercised on demand."""
    values = {"rain_mm_h": rain_mm_h, "wind_ms": wind_ms, "temp_c": temp_c,
              "cloud": None if cloud is None else cloud / 100.0}
    engine = engine_of(request)
    if all(v is None for v in values.values()):
        return engine.release_weather()
    return engine.override_weather({k: v for k, v in values.items() if v is not None}, hours)


@router.post("/sim/weather/release")
def sim_weather_release(request: Request) -> dict[str, Any]:
    return engine_of(request).release_weather()


@router.post("/sim/pests")
def sim_pests(request: Request,
              area: str | None = Query(None),
              plots: str | None = Query(None, description="comma separated plot ids"),
              pressure: float = Query(0.8, ge=0.0, le=0.99)) -> dict[str, Any]:
    """Seed a pest outbreak so the spray rule has something to act on."""
    engine = engine_of(request)
    ids = [p.strip() for p in (plots or "").split(",") if p.strip()] or None
    try:
        return engine.inject_pests(area, pressure, ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no matching plots for {exc.args[0]}")


@router.post("/sim/stress")
def sim_stress(request: Request,
               area: str | None = Query(None),
               kind: str = Query("dry", pattern="^(dry|wet)$")) -> dict[str, Any]:
    """Push plots into water stress or waterlogging, to drive the irrigation rule."""
    engine = engine_of(request)
    try:
        return engine.stress_plots(area, kind)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown area {exc.args[0]}")


@router.post("/sim/fault/{device_id}")
def sim_fault(request: Request, device_id: str,
              fault: str = Query("offline", description="online | offline | degraded")) -> dict[str, Any]:
    """Take a device off the bus, or put it back."""
    try:
        return engine_of(request).set_device_fault(device_id, fault)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown device {device_id}")


@router.post("/automation")
def set_automation(request: Request,
                   enabled: bool | None = None,
                   auto_spray: bool | None = None,
                   auto_irrigate: bool | None = None) -> dict[str, Any]:
    auto = engine_of(request).automation
    if enabled is not None:
        auto.enabled = enabled
    if auto_spray is not None:
        auto.auto_spray = auto_spray
    if auto_irrigate is not None:
        auto.auto_irrigate = auto_irrigate
    return {"enabled": auto.enabled, "auto_spray": auto.auto_spray,
            "auto_irrigate": auto.auto_irrigate}


@router.post("/irrigation/{zone_id}/open")
def open_zone(request: Request, zone_id: str,
              target_mm: float = Query(20.0, gt=0.0, le=80.0)) -> dict[str, Any]:
    engine = engine_of(request)
    zone = engine.irrigation.open_zone(zone_id, target_mm, engine.clock.now(), trigger="manual")
    if zone is None:
        raise HTTPException(status_code=409, detail="tank empty and borewell idle")
    return {"zone": zone.state(engine.clock.now())}


@router.post("/irrigation/{zone_id}/close")
def close_zone(request: Request, zone_id: str) -> dict[str, Any]:
    engine = engine_of(request)
    zone = engine.irrigation.close_zone(zone_id, engine.clock.now(), trigger="manual")
    if zone is None:
        raise HTTPException(status_code=404, detail=f"unknown zone {zone_id}")
    return {"zone": zone.state(engine.clock.now())}


@router.post("/drone/mission")
async def plan_mission(request: Request,
                       area: str | None = Query(None),
                       plots: str | None = Query(None, description="comma separated plot ids"),
                       dose_l_ha: float = Query(0.35, gt=0.0, le=5.0),
                       launch: bool = Query(True)) -> dict[str, Any]:
    engine = engine_of(request)
    if plots:
        ids = [p.strip() for p in plots.split(",") if p.strip()]
        targets = [engine.field.by_id[i] for i in ids if i in engine.field.by_id]
        if not targets:
            raise HTTPException(status_code=404, detail="no matching plots")
    elif area:
        targets = engine.field.plots_in_area(area)
        if not targets:
            raise HTTPException(status_code=404, detail=f"unknown area {area}")
    else:
        # Default: the worst offenders, which is what an operator would pick.
        targets = engine.field.worst_plots(6)
    w = engine.weather_state
    mission = engine.drone.plan_mission(targets, engine.clock.now(), trigger="manual",
                                         wind_dir=w.wind_dir, wind_speed=w.wind_speed,
                                         dose_l_ha=dose_l_ha)
    if launch:
        if not engine.drone.start(engine.clock.now()):
            raise HTTPException(status_code=409,
                                detail=f"drone cannot launch from state {engine.drone.state}")
    return {"mission": mission.as_dict(), "launched": launch}


@router.post("/drone/rth")
def rth(request: Request, reason: str = Query("manual")) -> dict[str, Any]:
    engine = engine_of(request)
    engine.drone.return_home(engine.clock.now(), reason)
    return {"state": engine.drone.state, "reason": reason}


@router.post("/drone/abort")
def abort(request: Request, reason: str = Query("manual")) -> dict[str, Any]:
    engine = engine_of(request)
    engine.drone.abort(engine.clock.now(), reason)
    return {"state": engine.drone.state, "reason": reason}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(request: Request, alert_id: int) -> dict[str, Any]:
    return {"ok": engine_of(request).ack_alert(alert_id), "alert_id": alert_id}


@router.post("/alerts/ack-all")
def ack_all(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    count = engine.automation.ack_all()
    engine.db and engine.db.submit("UPDATE alerts SET acked = 1 WHERE acked = 0")
    return {"ok": True, "cleared": count}


# --------------------------------------------------------------------------- #
# live channels
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    engine: SimulationEngine = ws.app.state.engine
    loop = asyncio.get_running_loop()
    period = 1.0 / WS_PUSH_HZ
    last_push = 0.0
    try:
        await ws.send_text(json.dumps({"type": "hello",
                                       "farm": engine.settings.farm_name,
                                       "speed": engine.clock.speed,
                                       "sim": engine.snapshot(False, False)["sim"]}))
        while True:
            # ``to_thread`` keeps the (CPU-bound, GIL-holding) snapshot off the
            # event loop so the WebSocket stays responsive under load.
            snap = await loop.run_in_executor(
                None, lambda: engine.snapshot(include_plots=True, include_history=True))
            snap["type"] = "state"
            await ws.send_text(json.dumps(snap, default=float))
            last_push = time.monotonic()
            # Honour the operator's speed control promptly.
            try:
                command = await asyncio.wait_for(ws.receive_text(), timeout=period)
            except asyncio.TimeoutError:
                continue
            _handle_ws_command(engine, command)
    except WebSocketDisconnect:
        return
    except (RuntimeError, ValueError):
        return


def _handle_ws_command(engine: SimulationEngine, raw: str) -> None:
    """Commands multiplexed over the WebSocket, so the UI needs one socket."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    cmd = msg.get("cmd")
    if cmd == "speed":
        engine.set_speed(float(msg.get("value", 1.0)))
    elif cmd == "pause":
        engine.set_paused(bool(msg.get("value", True)))
    elif cmd == "zone":
        zone, action = msg.get("zone"), msg.get("action")
        ts = engine.clock.now()
        if action == "open":
            engine.irrigation.open_zone(zone, float(msg.get("mm", 20.0)), ts, "dashboard")
        else:
            engine.irrigation.close_zone(zone, ts, "dashboard")
    elif cmd == "rth":
        engine.drone.return_home(engine.clock.now(), "dashboard")
    elif cmd == "ack":
        engine.ack_alert(int(msg.get("id", 0)))


@router.get("/cameras/{camera_id}/stream.mjpg")
def camera_stream(request: Request, camera_id: str,
                  fps: float = Query(2.0, ge=0.2, le=10.0)) -> StreamingResponse:
    """MJPEG stream for one camera (multipart/x-mixed-replace)."""
    engine = engine_of(request)
    if engine.cameras.get(camera_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown camera {camera_id}")
    boundary = "agriframe"

    async def frames():
        while not await request.is_disconnected():
            cam = engine.cameras.get(camera_id)
            frame = cam.last_frame if cam else None
            if frame:
                yield (b"--" + boundary.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                       + frame + b"\r\n")
            else:
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.0 / fps)

    return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={boundary}",
                             headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _loads(raw: Any) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def topic_reference() -> dict[str, str]:
    return {
        "soil": "agri/<farm>/soil/<probe>/telemetry",
        "weather": "agri/<farm>/weather/<station>/telemetry",
        "camera": "agri/<farm>/camera/<cam>/detections",
        "irrigation": "agri/<farm>/irrigation/<valve|system>/{state,event,telemetry}",
        "drone": "agri/<farm>/drone/<id>/{telemetry,mission,application,event}",
        "alerts": "agri/<farm>/system/alerts/{alert,escalation}",
        "rules": "agri/<farm>/system/rules/decision",
        "engine": "agri/<farm>/system/engine/event",
    }


def health_payload(engine: SimulationEngine, settings: Settings) -> dict[str, Any]:
    """Liveness/readiness probe (also used by ``/api/health``)."""
    return {"status": "ok", "farm": settings.farm_name,
            "sim": engine.snapshot(False, False)["sim"],
            "devices": len(engine.device_states()),
            "db": engine.db.stats() if engine.db else {}}
