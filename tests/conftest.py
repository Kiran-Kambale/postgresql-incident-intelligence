import os
import sys
import datetime as dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.demo import CYCLE_S, EPOCH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.timeutil import utcnow  # noqa: E402


def phase_offset_for(target_sec: float = 1500.0, now: dt.datetime | None = None) -> int:
    """Offset (seconds) that places 'now' at `target_sec` into the demo scenario cycle (1500 = deep inside the blocking phase)."""
    now = now or utcnow()
    return int((target_sec - ((now - EPOCH).total_seconds() % CYCLE_S)) % CYCLE_S)


def make_settings(tmp_path, **env):
    base = {"DEMO_MODE": "true", "AUTO_MONITOR": "false"}
    base.update({k: str(v) for k, v in env.items()})
    return Settings.from_env(base, data_dir=tmp_path)


@pytest.fixture
def demo_app(tmp_path, monkeypatch):
    """Demo app (separate SQLite file) with ~35 minutes of labelled synthetic history ending inside a blocking episode."""
    monkeypatch.setenv("DEMO_PHASE_OFFSET_S", str(phase_offset_for(1500)))
    app = create_app(make_settings(tmp_path), start_recorder=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        ctx = app.state.ctx
        ctx.recorder.demo.seed(ctx.recorder, hours=0.6)
        client.app_ctx = ctx
        yield client


@pytest.fixture
def empty_demo_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PHASE_OFFSET_S", str(phase_offset_for(100)))
    app = create_app(make_settings(tmp_path), start_recorder=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.app_ctx = app.state.ctx
        yield client


def auth_app(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("DEMO_PHASE_OFFSET_S", str(phase_offset_for(1500)))
    s = make_settings(tmp_path, AUTH_MODE="token", API_TOKENS="viewer:viewer-token-0123456789,operator:operator-token-012345,admin:admin-token-0123456789", **extra)
    return create_app(s, start_recorder=False)


@pytest.fixture
def secured(tmp_path, monkeypatch):
    app = auth_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        ctx = app.state.ctx
        ctx.recorder.demo.seed(ctx.recorder, hours=0.3)
        client.app_ctx = ctx
        yield client


H = lambda tok: {"Authorization": f"Bearer {tok}"}
