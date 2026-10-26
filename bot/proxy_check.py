"""On-demand check of a user's OWN proxies: one real HTTPS request each.

Surfaces a «🧪 Проверить прокси» button on the /proxy screen (own proxies
only). Each check is a real HTTPS GET through the proxy to Cloudflare's
``cdn-cgi/trace`` — that exercises CONNECT, TLS and the exit, and hands back
the exit IP, so a provider reselling one exit to ten lines is visible.

Design limits, all deliberate:

* **Own file only.** The shared pool is thousands of lines and is not the
  user's; a fan-out like that is what rule 6 on this box forbids. Shared-pool
  health stays the passive 24h ``user_proxy_health`` line.
* **First CHECK_LIMIT urls, never more.** A 1000-line file is not fully
  scanned; the report says it checked a prefix.
* **Report only.** Dead proxies are listed, nothing is deleted or rewritten —
  the file is the user's and stays theirs to fix.
* **One run per COOLDOWN_SEC per user**, in memory: the button must not
  become a private fan-out either.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from bot.http_client import proxy_dict_for

CHECK_URL = "https://www.cloudflare.com/cdn-cgi/trace"
CHECK_LIMIT = 20
CHECK_CONCURRENCY = 3
CHECK_TIMEOUT = (5, 8)
COOLDOWN_SEC = 300

_cooldown_lock = threading.Lock()
_last_run: dict[int, float] = {}
_report_lock = threading.Lock()
_reports: dict[int, dict] = {}


def start(user_id: int, urls: list[str], on_done=None) -> None:
    """Kick off a background check for this user and remember the report.

    The caller has already passed ``may_run`` (that is also what takes the
    cooldown slot). ``on_done(report_dict)`` runs in the worker thread — the
    Telegram screen redraw uses it; the mini app just polls ``last_report``.
    """
    uid = int(user_id)
    batch = [u for u in (urls or []) if u][:CHECK_LIMIT]
    with _report_lock:
        _reports[uid] = {"status": "running", "total": len(batch), "ts": time.time()}

    def _run() -> None:
        report = {
            "status": "done",
            "ts": time.time(),
            "total": len(batch),
            "results": run_check(batch, limit=len(batch) or 1),
        }
        with _report_lock:
            _reports[uid] = report
        if on_done is not None:
            try:
                on_done(report)
            except Exception:  # noqa: BLE001 — a failed redraw must not lose the report
                pass

    threading.Thread(target=_run, daemon=True).start()


def last_report(user_id: int) -> dict | None:
    with _report_lock:
        report = _reports.get(int(user_id))
        return dict(report) if report else None


def may_run(user_id: int) -> tuple[bool, int]:
    """Is this user allowed to start a check now? Returns ``(ok, wait_sec)``."""
    now = time.time()
    with _cooldown_lock:
        last = _last_run.get(int(user_id))
        if last is None:
            _last_run[int(user_id)] = now
            # Prune so the map cannot grow with the user base.
            if len(_last_run) > 10000:
                cutoff = now - COOLDOWN_SEC
                for uid, ts in list(_last_run.items()):
                    if ts < cutoff:
                        _last_run.pop(uid, None)
            return True, 0
        wait = int(last + COOLDOWN_SEC - now)
        if wait > 0:
            return False, wait
        _last_run[int(user_id)] = now
        return True, 0


def _label(url: str) -> str:
    """`host:port` for the report — the credential part never leaves the file."""
    try:
        parsed = urlparse(url)
        return f"{parsed.hostname}:{parsed.port}"
    except Exception:
        return "proxy"


def _check_one(url: str) -> dict:
    started = time.time()
    try:
        with requests.Session() as session:
            response = session.get(
                CHECK_URL,
                proxies=proxy_dict_for(url),
                timeout=CHECK_TIMEOUT,
            )
        ok = 200 <= response.status_code < 300
        ip = ""
        if ok:
            for line in (response.text or "").splitlines():
                if line.startswith("ip="):
                    ip = line[3:].strip()
                    break
        return {
            "label": _label(url),
            "ok": ok,
            "ip": ip,
            "ms": int((time.time() - started) * 1000),
            "error": "" if ok else f"HTTP {response.status_code}",
        }
    except Exception as exc:  # noqa: BLE001 — proxy error shapes vary
        return {
            "label": _label(url),
            "ok": False,
            "ip": "",
            "ms": int((time.time() - started) * 1000),
            "error": str(exc).split(":")[0][:60] or "error",
        }


def run_check(urls: list[str], limit: int = CHECK_LIMIT) -> list[dict]:
    """Check the first ``limit`` proxy urls, at most CHECK_CONCURRENCY at once."""
    batch = [u for u in (urls or []) if u][: max(1, int(limit))]
    if not batch:
        return []
    with ThreadPoolExecutor(max_workers=min(CHECK_CONCURRENCY, len(batch))) as pool:
        return list(pool.map(_check_one, batch))


def format_report(results: list[dict], total: int, lang: str = "ru") -> str:
    """HTML report for the proxy screen notice (credentials never included)."""
    from bot.i18n import t
    from bot.ux import esc

    checked = len(results)
    alive = sum(1 for r in results if r.get("ok"))
    head = t(
        "proxy.check_report",
        lang,
        alive=alive,
        checked=checked,
    )
    if checked < total:
        head += " · " + t("proxy.check_prefix", lang, checked=checked, total=total)
    lines = [head]
    for r in results:
        label = esc(str(r.get("label") or "proxy"))
        if r.get("ok"):
            ip = esc(str(r.get("ip") or "?"))
            lines.append(f"✅ <code>{label}</code> · {r.get('ms', 0)} ms · ip {ip}")
        else:
            err = esc(str(r.get("error") or "error")[:60])
            lines.append(f"❌ <code>{label}</code> — {err}")
    return "\n".join(lines)
