"""Read-only project health probes, cache and admin rendering."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="project_health_"))
os.environ["BOT_DB"] = str(_TMP / "health.db")
os.environ["BOT_RESULTS"] = str(_TMP / "results.jsonl")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import admin_ux  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import project_health as health  # noqa: E402

bot_db.init_db()


class _Response:
    def __init__(self, code: int, *, text: str = "", data=None):
        self.status_code = code
        self.text = text
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def _project(pid: str = "demo", **kwargs):
    base = {
        "id": pid,
        "name": "Demo",
        "health_url": "https://example.test/health",
        "health_method": "GET",
        "health_required_env_any": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _with_response(response: _Response, fn):
    original = health.requests.request
    health.requests.request = lambda *args, **kwargs: response
    try:
        return fn()
    finally:
        health.requests.request = original


def test_explicit_open_and_closed_json() -> None:
    opened = _with_response(
        _Response(200, data={"submissionsOpen": True}),
        lambda: health.probe_project(_project("open")),
    )
    assert opened.status == "open"
    closed = _with_response(
        _Response(200, data={"config": {"gateStatus": "CLOSED"}}),
        lambda: health.probe_project(_project("closed")),
    )
    assert closed.status == "closed"


def test_removed_endpoint_and_missing_config() -> None:
    gone = _with_response(
        _Response(404, text="Not Found"),
        lambda: health.probe_project(_project("gone")),
    )
    assert gone.status == "closed" and "404" in gone.detail

    os.environ.pop("HEALTH_TEST_KEY", None)
    missing = health.probe_project(
        _project("config", health_required_env_any=["HEALTH_TEST_KEY"])
    )
    assert missing.status == "config"


def test_cache_and_admin_panel() -> None:
    result = health.HealthResult(
        project_id="cached",
        status="reachable",
        detail="HTTP 200",
        checked_at="2026-08-11T01:00:00+00:00",
        http_status=200,
    )
    health.save_result(result)
    cached = health.get_cached("cached")
    assert cached is not None and cached["status"] == "reachable"
    project = _project("cached")
    panel = admin_ux.format_project_health_panel([project], {"cached": cached})
    assert "Здоровье проектов" in panel
    assert "сайт/API доступен" in panel


if __name__ == "__main__":
    test_explicit_open_and_closed_json()
    test_removed_endpoint_and_missing_config()
    test_cache_and_admin_panel()
    print("test_project_health: OK")
