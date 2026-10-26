"""Cached, read-only availability checks for configured projects."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from bot import db as bot_db
from bot.form_closed import is_form_closed_detail

_KEY_PREFIX = "project_health:"
_UA = "Mozilla/5.0 AutoregProjectHealth/1.0"


@dataclass(frozen=True)
class HealthResult:
    project_id: str
    status: str
    detail: str
    checked_at: str
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(
    project_id: str,
    status: str,
    detail: str,
    http_status: int | None = None,
) -> HealthResult:
    return HealthResult(
        project_id=project_id,
        status=status,
        detail=(detail or "")[:240],
        checked_at=_now(),
        http_status=http_status,
    )


def _find_json_key(value: Any, wanted: str) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() == wanted.lower():
                return child
        for child in value.values():
            found = _find_json_key(child, wanted)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_json_key(child, wanted)
            if found is not None:
                return found
    return None


def _explicit_json_state(data: Any) -> tuple[str, str] | None:
    for key in ("submissionsOpen", "applicationsOpen", "registrationOpen"):
        value = _find_json_key(data, key)
        if isinstance(value, bool):
            return (
                ("open", f"{key}=true")
                if value
                else ("closed", f"{key}=false")
            )
    # `{"state": {"kind": "live" | "preopening" | "closed"}}` — the shape
    # EtherBubu's `/api/campaign` uses. Read only from inside a `state` object
    # so a stray `kind` elsewhere in some other project's JSON cannot decide
    # whether its form is open.
    state = _find_json_key(data, "state")
    if isinstance(state, dict):
        kind = str(state.get("kind") or "").strip().lower()
        if kind in ("live", "open", "active"):
            return "open", f"state.kind={kind}"
        if kind in ("closed", "ended", "paused", "finished"):
            return "closed", f"state.kind={kind}"
        if kind in ("preopening", "pending", "scheduled"):
            # Not open yet: nothing can be filed, so submits stay blocked — but
            # this is not the form having ended, and it must not read as it.
            return "closed", f"state.kind={kind} (not open yet)"

    gate = _find_json_key(data, "gateStatus")
    if gate is not None:
        text = str(gate).strip().upper()
        if text in ("OPEN", "ACTIVE", "LIVE"):
            return "open", f"gateStatus={text}"
        if text in ("CLOSED", "PAUSED", "ENDED", "DISABLED"):
            return "closed", f"gateStatus={text}"
    return None


def probe_project(project: Any, *, timeout: float = 12.0) -> HealthResult:
    """Run one safe GET/HEAD/OPTIONS probe; never submits registration data."""
    pid = str(project.id)
    required = list(getattr(project, "health_required_env_any", None) or [])
    if required and not any(os.getenv(name, "").strip() for name in required):
        return _result(pid, "config", "Не настроен обязательный внешний сервис")

    url = str(getattr(project, "health_url", "") or "").strip()
    if not url:
        return _result(pid, "unknown", "Health URL не настроен")
    method = str(getattr(project, "health_method", "GET") or "GET").upper()
    if method not in ("GET", "HEAD", "OPTIONS"):
        return _result(pid, "error", "Небезопасный health method отклонён")

    try:
        response = requests.request(
            method,
            url,
            timeout=max(1.0, float(timeout)),
            allow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "application/json,text/html,*/*"},
        )
    except requests.RequestException as exc:
        return _result(pid, "error", f"{type(exc).__name__}: {exc}")

    code = int(response.status_code)
    body = (response.text or "")[:20000]
    if code in (404, 410):
        return _result(pid, "closed", f"HTTP {code}: endpoint unavailable", code)
    if code >= 400:
        return _result(pid, "error", f"HTTP {code}", code)

    data: Any = None
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        data = None
    if data is not None:
        explicit = _explicit_json_state(data)
        if explicit is not None:
            return _result(pid, explicit[0], explicit[1], code)

    plain = re.sub(r"\s+", " ", body).strip()
    if is_form_closed_detail(plain[:5000], "BAD_RESP"):
        return _result(pid, "closed", "Страница сообщает о закрытой регистрации", code)
    return _result(pid, "reachable", f"HTTP {code}, endpoint доступен", code)


def save_result(result: HealthResult) -> None:
    bot_db.set_setting(
        f"{_KEY_PREFIX}{result.project_id}",
        json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")),
    )


def get_cached(project_id: str) -> dict[str, Any] | None:
    raw = bot_db.get_setting(f"{_KEY_PREFIX}{project_id}", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def cached_health_map(projects: Iterable[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for project in projects:
        cached = get_cached(str(project.id))
        if cached is not None:
            out[str(project.id)] = cached
    return out


def refresh_projects(
    projects: Iterable[Any], *, max_workers: int = 6
) -> dict[str, dict[str, Any]]:
    items = list(projects)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items) or 1))) as pool:
        futures = {pool.submit(probe_project, project): project for project in items}
        for future in as_completed(futures):
            project = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive: one probe must not break the panel
                result = _result(
                    str(project.id), "error", f"{type(exc).__name__}: {exc}"
                )
            save_result(result)
            results[result.project_id] = result.to_dict()
    return results


def status_icon(status: str | None) -> str:
    # User-facing project picker intentionally has only two health icons:
    # confirmed open and not confirmed/open status unknown.
    if str(status or "").lower() in ("open", "reachable"):
        return "🟢"
    return "⚪"
