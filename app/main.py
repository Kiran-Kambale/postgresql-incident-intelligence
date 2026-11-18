"""DB Incident Replay: application factory, lifecycle and static dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import ROOT, TUNABLES, Settings
from .context import AppContext
from .demo import DemoSource
from .errors import install_handlers
from .recorder import Recorder
from .security import SECURITY_HEADERS, setup_logging
from .store import Store

log = logging.getLogger("dir.app")
STATIC = ROOT / "static"


def create_app(settings: Settings | None = None, *, start_recorder: bool | None = None) -> FastAPI:
    load_dotenv(ROOT / ".env")
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / ("demo_history.sqlite3" if settings.demo_mode else "incident_history.sqlite3"))
    for name, raw in store.settings_load().items():           # runtime overrides saved from the Monitoring Settings page
        if name in TUNABLES:
            try:
                settings.set_override(name, raw)
            except ValueError:
                log.warning("ignoring invalid stored setting %s", name)
    recorder = Recorder(settings, store, DemoSource() if settings.demo_mode else None)
    run_recorder = settings.auto_monitor if start_recorder is None else start_recorder
    for w in settings.config_warnings:
        log.warning(w)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = AppContext(settings, store, recorder)
        if run_recorder and settings.instances:
            if settings.demo_mode and not store.kv_get("demo_seeded_at"):
                log.info("seeding labelled synthetic demo history (first start only)")
                await asyncio.to_thread(recorder.demo.seed, recorder)      # finish before live collection starts so time stays ordered
            await recorder.start()
        try:
            yield
        finally:
            await recorder.stop()

    app = FastAPI(title="DB Incident Replay", version=__version__, lifespan=lifespan,
                  description="PostgreSQL production incident intelligence: flight-recorder evidence, blocking analysis, incident workflow. Read-only diagnostics.")
    app.state.ctx = AppContext(settings, store, recorder)      # available before lifespan for tests that skip it
    install_handlers(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    from .api import diagnostics, investigation, legacy, system
    for r in (system.router, diagnostics.router, investigation.router, legacy.router):
        app.include_router(r)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """`uvicorn app.main:app` creates the application on first access, so importing this module (tests, tooling) has no side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
