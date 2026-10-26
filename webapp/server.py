#!/usr/bin/env python3
"""Telegram Mini App server: this user's queue, and the buttons that change it.

It runs two ways, and the difference is not cosmetic.

*Standalone* (``python webapp/server.py --port 8081``) it is what it always
was: its own process, the database opened read-only, one screen, no way to
change anything. Useful for looking at the UI without a bot running.

*In-process* (``serve_in_thread`` from ``tgbot.main``) it can start and cancel
work — because that is the only place it can. The submit queue lives in the
bot process's memory, so a job inserted from anywhere else is a row no worker
will ever pop. See ``webapp/actions.py`` for the full reasoning.

Which mode is in force is decided by the ``Actions`` object, not by a flag read
here. Nothing in this file knows how to start a job; it knows how to
authenticate a caller, refuse a flood, and hand the request to something that
does.

Routes::

    GET  /                the screen
    POST /api/state       queue + projects for the caller
    POST /api/start       {"project_id": "..."} -> start it
    POST /api/start_all   VIP: one job per eligible project
    POST /api/cancel      {"job_id": N} -> cancel that job
    POST /api/data        the accounts .txt, raw; credential in X-Init-Data
    POST /api/data/clear  delete the accounts file
    POST /api/proxies     the proxy .txt, raw; credential in X-Init-Data
    POST /api/proxies/clear

``initData`` never goes in a query string: those land in access logs and
browser history, and this one is a credential. For the small routes it rides in
the JSON body; for the two uploads it rides in a header instead, so the caller
can be verified before half a megabyte of body is read off the socket. Every
write answers with the fresh state as well, so the screen after a tap reflects
the tap without a second round trip that could race it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Nothing is pre-seeded into the environment here. `bot/config.py` calls
# `load_dotenv`, which by design does **not** overwrite a variable that already
# exists — so setting `BOT_TOKEN=""` as a "safe default" before the import
# would silently stop the real token in `.env` from ever loading, and every
# request would answer 503. Found exactly that way on first deploy.
from bot import config  # noqa: E402
from bot.registry import list_projects, load_projects  # noqa: E402
from webapp import ratelimit  # noqa: E402
from webapp.actions import Actions, Denied, ReadOnly  # noqa: E402
from webapp.auth import AuthError, user_from  # noqa: E402
from webapp.state import build_state  # noqa: E402

INDEX = Path(__file__).with_name("index.html")
MAX_BODY = 16 * 1024

# How many requests may be *inside* a handler at once. Everything above this
# waits for a slot and is answered 503 rather than getting a thread of its own.
#
# The number matters more here than it would in a normal web server, because
# these threads are not in a web server: they are in the bot process, beside
# the queue workers and the Telegram handlers, all under one GIL. A flood that
# would merely slow a standalone service down stalls submissions here — and
# `ThreadingHTTPServer` by itself will start a thread for every connection the
# network offers it. Sixteen sits comfortably above real demand (a screen polls
# every four seconds and a read is tens of milliseconds) and well below the
# point where the queue would feel it.
MAX_IN_FLIGHT = 16
# How long a request waits for a slot before being told to come back. Long
# enough to ride out a burst, short enough that the caller is still listening.
IN_FLIGHT_WAIT_S = 5.0
_IN_FLIGHT = threading.BoundedSemaphore(MAX_IN_FLIGHT)

# The page is a fixed asset and was being read off disk on every single GET.
# Cached on (mtime_ns, size) rather than for a duration, so an edit still lands
# without a restart and there is no staleness window to reason about.
_index_lock = threading.Lock()
_index_cache: tuple[tuple[int, int], bytes, str] | None = None


def _index_load() -> tuple[bytes, str] | None:
    global _index_cache
    try:
        stat = INDEX.stat()
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _index_lock:
        hit = _index_cache
        if hit is not None and hit[0] == stamp:
            return hit[1], hit[2]
    try:
        body = INDEX.read_bytes()
    except OSError:
        return None
    tag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
    with _index_lock:
        _index_cache = (stamp, body, tag)
    return body, tag


def _index_bytes() -> bytes | None:
    loaded = _index_load()
    return loaded[0] if loaded else None


def _index_etag() -> str:
    loaded = _index_load()
    return loaded[1] if loaded else ""

# Uploads are the one thing that legitimately needs a large body: an accounts
# file is a thousand lines, a proxy list the same. The cap follows the bot's
# own ceilings rather than a number invented here, with room for the encoding
# the browser used. Only the two upload routes get it; everything else stays at
# MAX_BODY, so a flood cannot ask for a megabyte by picking a different path.
def _upload_cap() -> int:
    biggest = max(
        int(getattr(config, "USER_DATA_MAX_BYTES", 512 * 1024) or 0),
        int(getattr(config, "USER_PROXY_MAX_BYTES", 512 * 1024) or 0),
        64 * 1024,
    )
    return biggest + 8 * 1024

# Replaced by `serve_in_thread`. The default refuses every write, so a server
# started without one cannot quietly acquire the ability to change things.
ACTIONS: Actions = ReadOnly()


def project_names() -> dict[str, str]:
    """id -> display name, so the screen never shows a raw project id."""
    try:
        load_projects()
        return {p.id: p.name for p in list_projects()}
    except Exception:  # noqa: BLE001 — a naming lookup must not take the page down
        return {}


def _state_for(uid: int) -> dict[str, Any]:
    """Queue view plus the project list, in one payload.

    The two come from different places — the queue is read out of the database,
    the project rows are computed by the bot's own readiness gate — but the
    screen needs both to draw one row, so they travel together. Fetching them
    separately would let the page show a project as startable next to a job
    that already started it.
    """
    lang = ACTIONS.lang_of(uid)
    state = build_state(config.DB_PATH, uid, project_names())
    state["lang"] = lang
    projects = ACTIONS.projects(uid, lang)
    state["projects"] = projects
    state["caps"] = ACTIONS.caps(uid, lang, projects=projects)
    state["data"] = ACTIONS.data(uid, lang)
    state["proxies"] = ACTIONS.proxies(uid, lang)
    state["notice"] = ACTIONS.notice(uid, lang)
    state["mints"] = ACTIONS.mints(uid, lang)
    state["gforms"] = ACTIONS.gforms(uid, lang)
    state["billing"] = ACTIONS.billing(uid, lang)
    state["privatka"] = ACTIONS.privatka(uid, lang)
    state["report"] = ACTIONS.report(uid, lang)
    state["setup"] = ACTIONS.user_setup(uid)
    state["can_write"] = bool(ACTIONS.may_write(uid))
    state["read_only_reason"] = "" if state["can_write"] else ACTIONS.read_only_reason
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "autoreg-webapp/1"

    # Without this the socket has no deadline, and `rfile.read(length)` waits
    # forever for bytes that never come. A client that announces a Content-Length
    # larger than what it sends — by accident on a dropped mobile connection, or
    # on purpose — would pin one thread per connection until the process dies.
    # These threads live inside the bot, so that is not just a web outage.
    timeout = 30

    # The default logger writes a line per request to stderr including the
    # query string; quiet it and log only what is useful.
    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/"):
            print(f"[webapp] {self.command} {self.path} {args[1] if len(args) > 1 else ''}",
                  flush=True)

    def _peer(self) -> str:
        """The socket's own address, for when the edge header is missing."""
        try:
            return str(self.client_address[0])
        except (AttributeError, IndexError, TypeError):
            return ""

    def _send(self, code: int, body: bytes, content_type: str,
              *, etag: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The page loads only Telegram's SDK and its own inline styles.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' https://telegram.org "
            "'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
        )
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The phone went away mid-answer: screen locked, network switched,
            # app closed. Normal on mobile and nothing to do about it — but it
            # printed a twenty-line traceback per occurrence, which buries the
            # entries that mean something.
            pass

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if not _IN_FLIGHT.acquire(timeout=IN_FLIGHT_WAIT_S):
            self._json(503, {"error": "busy"})
            return
        try:
            self._get()
        finally:
            _IN_FLIGHT.release()

    def do_POST(self) -> None:  # noqa: N802
        if not _IN_FLIGHT.acquire(timeout=IN_FLIGHT_WAIT_S):
            self._json(503, {"error": "busy"})
            return
        try:
            self._post()
        finally:
            _IN_FLIGHT.release()

    def _get(self) -> None:
        path = self.path.split("?", 1)[0]
        # The same window every POST goes through. This was the one route
        # without it, which made the cheapest request on the host — no
        # signature to verify, no database to read — also the only unmetered
        # one. An open hostname finds that.
        if not ratelimit.by_ip.allow(ratelimit.client_ip(self.headers, self._peer())):
            self._json(429, {"error": "rate limited"})
            return
        if path in ("/", "/index.html"):
            body = _index_bytes()
            if body is None:
                self._json(500, {"error": "index missing"})
                return
            # Read once, not once per request: this is a fixed 48 KB asset and
            # every poll-heavy client fetches it on open. `_index_bytes` keys
            # its copy on (mtime, size), so editing the file still takes effect
            # without a restart.
            if self.headers.get("If-None-Match") == _index_etag():
                self._send(304, b"", "text/html; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8",
                       etag=_index_etag())
            return
        self._json(404, {"error": "not found"})

    # ---- POST routing -----------------------------------------------------

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _authenticate(self, init_data: str) -> int | None:
        """Shared front door: token present, signature valid, budgets not spent.

        Returns the verified user id, or None after having already answered.
        """
        token = str(getattr(config, "BOT_TOKEN", "") or "")
        if not token:
            # Without the token nothing can be verified, and serving data
            # unverified is the one thing this must never do.
            self._json(503, {"error": "server not configured"})
            return None
        try:
            who = user_from(init_data, token)
        except AuthError:
            self._json(401, {"error": "bad initData"})
            return None
        uid = who["id"]
        if not ratelimit.by_user.allow(str(uid)):
            # A real page polls every 4 s; a verified session going much faster
            # is a bug or a replayed initData. Either way, not the database's
            # problem — and this now runs *before* `ensure_user`, because that
            # is a write, and letting a replayed initData spend the IP budget's
            # worth of writes before the per-user window closed defeated the
            # point of having a per-user window.
            self._json(429, {"error": "rate limited"})
            return None
        # What every chat handler does on its first line. Without it a person
        # who opened the app without ever pressing /start has no `users` row,
        # and the first write with a foreign key onto it fails — reported, in
        # the case that surfaced this, as "one suggestion per day".
        ACTIONS.ensure_user(uid, who["username"], who["first_name"])
        # Ban and channel membership. The signature above proves who is asking;
        # this decides whether they may ask. Everything past this line was
        # reachable by a banned account and by someone who never joined the
        # channel, because the chat's gates lived in the chat handlers and the
        # web never had an equivalent.
        refusal = ACTIONS.gate(uid, ACTIONS.lang_of(uid))
        if refusal is not None:
            self._json(403, {"error": "forbidden", "code": refusal.code,
                             "message": refusal.message})
            return None
        return uid

    def _post(self) -> None:
        path = self.path.split("?", 1)[0]
        # Uploads carry their payload as the raw body and their credential in a
        # header, because the order matters: a JSON body would have to be read
        # in full — half a megabyte of it — before the signature inside could
        # say whether this caller is anyone at all. With the header the verify
        # and the write budget both happen first, and an unauthenticated flood
        # never gets to hand us the megabyte.
        uploads = {
            "/api/data": self._route_save_data,
            "/api/proxies": self._route_save_proxies,
        }
        if path in uploads:
            self._handle_upload(path, uploads[path])
            return

        route: Callable[[int, dict], dict] | None = {
            "/api/state": self._route_state,
            "/api/start": self._route_start,
            "/api/start_all": self._route_start_all,
            "/api/cancel": self._route_cancel,
            "/api/data/clear": self._route_clear_data,
            "/api/data/delete_row": self._route_delete_data_row,
            "/api/results": self._route_results,
            "/api/stats": self._route_stats,
            "/api/proxies/clear": self._route_clear_proxies,
            "/api/proxies/check": self._route_check_proxies,
            "/api/lang": self._route_lang,
            "/api/keys": self._route_set_captcha_key,
            "/api/keys/clear": self._route_clear_captcha_key,
            "/api/autoreg": self._route_set_auto_reg,
            "/api/mintremind": self._route_set_mint_remind,
            "/api/buy": self._route_buy,
            "/api/pay/check": self._route_pay_check,
            "/api/pay/cancel": self._route_pay_cancel,
            "/api/deliver": self._route_deliver,
            "/api/suggest": self._route_suggest,
            "/api/gform/parse": self._route_gform_parse,
            "/api/gform/create": self._route_gform_create,
            "/api/gform/delete": self._route_gform_delete,
            "/api/mint": self._route_save_mint,
            "/api/mint/clear": self._route_clear_mint,
        }.get(path)
        if route is None:
            self._json(404, {"error": "not found"})
            return

        # Before the HMAC, not after: an unauthenticated flood is what an open
        # hostname invites, and the cheapest place to refuse it is here. The
        # edge has its own rule, but this one cannot be switched off in a
        # dashboard this process knows nothing about.
        peer = ratelimit.client_ip(self.headers, self._peer())
        if not ratelimit.by_ip.allow(peer):
            self._json(429, {"error": "rate limited"})
            return

        payload = self._read_body()
        if payload is None:
            self._json(400, {"error": "bad request"})
            return

        uid = self._authenticate(str(payload.get("initData") or ""))
        if uid is None:
            return

        # Writes get their own, much tighter budget. Reads are a poll and are
        # meant to be frequent; a tap that starts real work up a proxy is not,
        # and the two must not share an allowance — otherwise a fast-polling
        # screen would spend the budget that protects the queue.
        if path != "/api/state" and not ratelimit.by_write.allow(str(uid)):
            self._json(429, {"error": "too many actions"})
            return

        try:
            result = route(uid, payload)
        except Denied as exc:
            # A gate said no in a way the user should read: wrong tier, no
            # slots left, form closed. 200 with ok=false, because nothing went
            # wrong — the answer is simply "not this".
            self._json(200, {"ok": False, "code": exc.code, "message": exc.message,
                             "state": self._safe_state(uid)})
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[webapp] {path} error: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, result)

    def _safe_state(self, uid: int) -> dict | None:
        """State for the refusal payload; never the reason a refusal fails."""
        try:
            return _state_for(uid)
        except Exception:  # noqa: BLE001
            return None

    def _handle_upload(self, path: str, route: Callable[[int, str], dict]) -> None:
        """A file, sent as itself: credential in the header, content in the body.

        Everything that can refuse this caller runs before a single byte of the
        body is read — IP window, signature, per-user window, write budget, and
        the rollout gate. Only then does the socket hand over the payload.
        """
        peer = ratelimit.client_ip(self.headers, self._peer())
        if not ratelimit.by_ip.allow(peer):
            self._json(429, {"error": "rate limited"})
            return
        uid = self._authenticate(self.headers.get("X-Init-Data") or "")
        if uid is None:
            return
        if not ratelimit.by_write.allow(str(uid)):
            self._json(429, {"error": "too many actions"})
            return
        if not ACTIONS.may_write(uid):
            self._json(200, {"ok": False, "code": "read_only",
                             "message": ACTIONS.read_only_reason,
                             "state": self._safe_state(uid)})
            return

        cap = _upload_cap()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "empty upload"})
            return
        if length > cap:
            # Say the number. "Too big" without it sends the user to guess how
            # much of a thousand-line file to cut.
            self._json(200, {"ok": False, "code": "too_big",
                             "message": f"Файл больше {cap // 1024} КБ — столько не принимаем.",
                             "state": self._safe_state(uid)})
            return
        raw = self.rfile.read(length)
        text = None
        for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            self._json(200, {"ok": False, "code": "encoding",
                             "message": "Не удалось прочитать файл — неизвестная кодировка.",
                             "state": self._safe_state(uid)})
            return

        try:
            result = route(uid, text)
        except Denied as exc:
            self._json(200, {"ok": False, "code": exc.code, "message": exc.message,
                             "state": self._safe_state(uid)})
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[webapp] {path} error: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"error": "unavailable"})
            return
        result["state"] = _state_for(uid)
        self._json(200, result)

    # ---- routes -----------------------------------------------------------

    def _route_state(self, uid: int, payload: dict) -> dict:
        del payload
        return _state_for(uid)

    def _guard_write(self, uid: int) -> None:
        if not ACTIONS.may_write(uid):
            raise Denied("read_only", ACTIONS.read_only_reason)

    def _route_start(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            raise Denied("bad_request", "не указан проект")
        out = ACTIONS.start(uid, project_id, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_start_all(self, uid: int, payload: dict) -> dict:
        del payload
        self._guard_write(uid)
        out = ACTIONS.start_all(uid, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_cancel(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        try:
            job_id = int(payload.get("job_id"))
        except (TypeError, ValueError):
            raise Denied("bad_request", "не указана задача") from None
        out = ACTIONS.cancel(uid, job_id, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    # Uploads: `_handle_upload` has already verified the caller and checked the
    # rollout gate, so these two only convert text into a stored file.
    def _route_save_data(self, uid: int, text: str) -> dict:
        return ACTIONS.save_data(uid, text, ACTIONS.lang_of(uid))

    def _route_save_proxies(self, uid: int, text: str) -> dict:
        return ACTIONS.save_proxies(uid, text, ACTIONS.lang_of(uid))

    def _route_clear_data(self, uid: int, payload: dict) -> dict:
        del payload
        self._guard_write(uid)
        out = ACTIONS.clear_data(uid, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_delete_data_row(self, uid: int, payload: dict) -> dict:
        row = payload.get("row")
        if not isinstance(row, int) or not 1 <= row <= 100000:
            raise Denied("bad_request", "номер строки: 1..100000")
        self._guard_write(uid)
        out = ACTIONS.delete_data_row(uid, row, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_stats(self, uid: int, payload: dict) -> dict:
        days = payload.get("days", 7)
        if days not in (7, 30):
            days = 7
        out = ACTIONS.stats(uid, int(days), ACTIONS.lang_of(uid))
        return out

    def _route_results(self, uid: int, payload: dict) -> dict:
        # Read-only in nature; the write limiter still applies because the
        # payload carries the user's wallet list out on every call.
        pid = payload.get("project_id")
        if not isinstance(pid, str) or not pid.strip() or len(pid) > 64:
            raise Denied("bad_request", "project_id")
        out = ACTIONS.export_results(uid, pid.strip(), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_clear_proxies(self, uid: int, payload: dict) -> dict:
        del payload
        self._guard_write(uid)
        out = ACTIONS.clear_proxies(uid, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_check_proxies(self, uid: int, payload: dict) -> dict:
        # Starts the background own-proxy check; the report itself travels
        # back on the state poll (`proxies.check`), so this stays a fast
        # answer even though the check takes up to a minute.
        del payload
        self._guard_write(uid)
        out = ACTIONS.check_proxies(uid, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _text(self, payload: dict, key: str, *, limit: int = 2000) -> str:
        """One field, bounded. A JSON string has no natural length."""
        value = payload.get(key)
        if not isinstance(value, str):
            raise Denied("bad_request", f"не указано: {key}")
        value = value.strip()
        if not value or len(value) > limit:
            raise Denied("bad_request", f"не указано: {key}")
        return value

    def _route_lang(self, uid: int, payload: dict) -> dict:
        # Language is the one setting that is not a capability: it changes what
        # this user reads, nothing else, so it is not behind the rollout gate.
        out = ACTIONS.set_lang(uid, self._text(payload, "lang", limit=8))
        out["state"] = _state_for(uid)
        return out

    def _route_set_captcha_key(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.set_captcha_key(
            uid,
            self._text(payload, "provider", limit=32),
            # A solver key is short; JSON body, not the raw-upload path.
            self._text(payload, "key", limit=128),
            ACTIONS.lang_of(uid),
        )
        out["state"] = _state_for(uid)
        return out

    def _route_clear_captcha_key(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.clear_captcha_key(uid, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_set_auto_reg(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.set_auto_reg(
            uid, str(payload.get("on") or "").lower() in ("1", "true", "yes", "on"),
            ACTIONS.lang_of(uid),
        )
        out["state"] = _state_for(uid)
        return out

    def _route_set_mint_remind(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.set_mint_remind(
            uid, str(payload.get("on") or "").lower() in ("1", "true", "yes", "on"),
            ACTIONS.lang_of(uid),
        )
        out["state"] = _state_for(uid)
        return out

    def _route_buy(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.buy(uid, self._text(payload, "plan", limit=32), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_pay_check(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.check_payment(
            uid, self._text(payload, "payment_id", limit=64), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_pay_cancel(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.cancel_payment(
            uid, self._text(payload, "payment_id", limit=64), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_deliver(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.deliver(
            uid, self._text(payload, "project_id", limit=64), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_save_mint(self, uid: int, payload: dict) -> dict:
        # No `_guard_write`: editing the calendar is not editing your own
        # account, it is publishing to everyone. `save_mint` checks admin.
        body = payload.get("mint")
        if not isinstance(body, dict):
            raise Denied("bad_request", "нет данных о минте")
        out = ACTIONS.save_mint(uid, body, ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_clear_mint(self, uid: int, payload: dict) -> dict:
        out = ACTIONS.clear_mint(uid, self._text(payload, "project_id", limit=64),
                                 ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_suggest(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.suggest(uid, self._text(payload, "text", limit=1000),
                              ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_gform_parse(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.gform_parse(
            uid, self._text(payload, "url", limit=400), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out

    def _route_gform_create(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        mapping = payload.get("mapping")
        if not isinstance(mapping, dict) or len(mapping) > 64:
            raise Denied("bad_request", "нет маппинга полей")
        # Values are small source strings, not free text: bound each one so a
        # mapping cannot smuggle a payload through the JSON route.
        bounded = {}
        for k, v in mapping.items():
            if not isinstance(k, str) or len(k) > 32:
                continue
            if isinstance(v, str) and len(v) <= 200:
                bounded[k] = v
        name = str(payload.get("name") or "")[:60]
        link = str(payload.get("link") or "")[:200]
        out = ACTIONS.gform_create(uid, bounded, ACTIONS.lang_of(uid),
                                   name=name, link=link)
        out["state"] = _state_for(uid)
        return out

    def _route_gform_delete(self, uid: int, payload: dict) -> dict:
        self._guard_write(uid)
        out = ACTIONS.gform_delete(
            uid, self._text(payload, "project_id", limit=64), ACTIONS.lang_of(uid))
        out["state"] = _state_for(uid)
        return out


class _QuietServer(ThreadingHTTPServer):
    """Same server, minus the traceback for a client that hung up.

    Two defaults are overridden rather than inherited.

    ``request_queue_size`` is 5 in the standard library, and that is the
    ``listen()`` backlog: the sixth connection arriving before the fifth is
    accepted is dropped by the kernel — no 429, no log line, just a client that
    sees a reset. Behind a tunnel that multiplexes many requests onto one
    connection, five is a number that gets reached by ordinary traffic.

    ``daemon_threads`` stays on, but the pool is no longer unbounded: see
    ``Handler._in_flight``.
    """

    request_queue_size = 128

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    return _QuietServer((host, port), Handler)


def serve_in_thread(
    host: str = "127.0.0.1", port: int = 8081, *, actions: Actions
) -> ThreadingHTTPServer:
    """Run the app inside the caller's process, with real capabilities.

    Returns the server so the caller can shut it down; the thread is a daemon
    because the bot's own exit path is what should decide when the process
    ends, not a socket with an open connection.
    """
    global ACTIONS
    ACTIONS = actions
    server = build_server(host, port)
    threading.Thread(
        target=server.serve_forever,
        name="webapp-http",
        daemon=True,
    ).start()
    print(f"[webapp] listening on http://{host}:{port} "
          f"(writes: {'on' if actions.writable else 'off'})", flush=True)
    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep it local and put TLS in front")
    ap.add_argument("--port", type=int, default=8081)
    args = ap.parse_args()

    if not str(getattr(config, "BOT_TOKEN", "") or ""):
        print("[webapp] BOT_TOKEN is not set — every call will be refused", flush=True)
    print("[webapp] standalone: read-only. Start/cancel need the bot process — "
          "the submit queue lives in its memory.", flush=True)
    server = build_server(args.host, args.port)
    print(f"[webapp] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
