"""AgriSense application entry point.

Wires the simulation engine to the HTTP layer and serves the dashboard.  The
engine runs on its own thread so a slow snapshot or a camera frame never blocks
request handling.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import health_payload, router
from .config import Settings
from .sim.engine import SimulationEngine

log = logging.getLogger("agrisense")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    engine = SimulationEngine(settings)
    app.state.engine = engine
    engine.start()
    log.info("AgriSense %s started (speed=%sx, bus=%s)", settings.farm_name,
             settings.default_speed, engine.transport.info()["mode"])
    try:
        yield
    finally:
        engine.stop()
        log.info("simulation engine stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="AgriSense", version="1.0.0", lifespan=lifespan,
                  description="Precision-farming IoT platform: soil telemetry, plant "
                              "cameras, automated irrigation and a pesticide drone.")
    app.state.settings = settings
    app.include_router(router)

    @app.get("/api/health")
    def health(request: Request) -> Any:
        return health_payload(request.app.state.engine, request.app.state.settings)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        # Two pages, deliberately: the dashboard is for watching the farm, the
        # simulation console is for changing it.  Neither page is a superset of
        # the other, so splitting them keeps each one readable.
        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "dashboard.html")

        @app.get("/dashboard", include_in_schema=False)
        def dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "dashboard.html")

        @app.get("/simulation", include_in_schema=False)
        def simulation() -> FileResponse:
            return FileResponse(STATIC_DIR / "simulation.html")

        @app.get("/favicon.svg", include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app


app = create_app()


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500,
                        content={"error": "internal_error", "detail": str(exc)})
