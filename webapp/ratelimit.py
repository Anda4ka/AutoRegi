"""Rate limiting at the origin, not only at the edge.

Cloudflare rules can be added, changed or accidentally removed in a dashboard
this code knows nothing about, and a prototype should not depend on a control
it cannot see. So the limits live here too: whatever the edge does, the origin
refuses to be hammered.

Three windows, because they defend different things:

* **per client IP, before verification** — the HMAC is cheap but not free, and
  an unauthenticated flood is the thing an open hostname invites. This is the
  gate that costs an attacker something.
* **per Telegram user, after verification** — a real client polls every 4 s.
  A verified session asking far faster is either a bug or someone replaying a
  captured `initData`, and neither deserves the database.
* **per Telegram user, writes only** — starting a job is not a poll. It spends
  proxy requests and a project's patience, so it gets a budget of its own.

The client address comes from `CF-Connecting-IP`. Trusting a header is normally
wrong, and it is only correct here because the server binds to `127.0.0.1` and
the sole path to it is the tunnel on the same host — nothing else can set it.
If that ever changes, this assumption has to change with it.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# A page polls every 4 s, i.e. ~15 requests a minute each.
#
# The per-IP number has to be generous, and the first version was not: at 60/min
# it allowed four simultaneous clients behind one address, while mobile carriers
# put thousands of subscribers behind a single CGNAT IP. That limit would have
# been a rule against real users, not against a flood. It exists only to bound
# an unauthenticated flood — verification is microseconds and the database is
# never touched before it — so 300/min still does that job while leaving room
# for twenty phones on one carrier address.
#
# The meaningful limit is the per-user one: it is keyed on a verified Telegram
# id, so NAT cannot make two people look like one, and 40/min is already almost
# three times what an open page needs.
IP_LIMIT, IP_WINDOW_S = 300, 60.0
# 40 was too tight and the load check found it: one screen polls ~15 times a
# minute, so a third open screen — a second phone, a reopened app, a stale tab —
# started collecting 429s from its own owner. The read path is 58 ms of work
# against a WAL database and is not what needs defending; the write budget
# below is. 120 leaves room for eight screens at once.
USER_LIMIT, USER_WINDOW_S = 120, 60.0
# Actions that change something get their own, far smaller budget, and it is
# deliberately not carved out of the polling one. A screen refreshing every 4 s
# is normal; a session starting jobs twelve times a minute is not, and if the
# two shared an allowance a chatty page would spend the protection that stands
# in front of the queue. Every start here costs real proxy requests downstream,
# so this is the number that decides how much damage one replayed `initData`
# can do before the window closes.
WRITE_LIMIT, WRITE_WINDOW_S = 12, 60.0
# Beyond this many tracked keys the oldest are dropped; a flood from many
# addresses must not turn into unbounded memory.
MAX_KEYS = 20000


class SlidingWindow:
    def __init__(self, limit: int, window_s: float):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if len(self._hits) > MAX_KEYS:
                self._evict_locked(moment)
            hits = self._hits[key]
            cutoff = moment - self.window_s
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            return True

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self.window_s
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) > MAX_KEYS:
            # Still too many live keys: drop the least recently seen half
            # rather than grow without bound.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
            for key, _v in ordered[: len(ordered) // 2]:
                del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


by_ip = SlidingWindow(IP_LIMIT, IP_WINDOW_S)
by_user = SlidingWindow(USER_LIMIT, USER_WINDOW_S)
by_write = SlidingWindow(WRITE_LIMIT, WRITE_WINDOW_S)


def client_ip(headers, peer: str = "") -> str:
    """Caller address as Cloudflare reports it; see the module docstring.

    ``peer`` is the socket's own address, used when the header is absent. It
    used to fall back to the literal string ``"unknown"``, which is one bucket:
    every header-less request in the world shared a single 300/minute window,
    so one source could spend the allowance of all the others. The socket
    address is not spoofable from outside and gives each of them their own.

    ``X-Forwarded-For`` is honored only when the socket peer is loopback —
    the local tunnel. On any other bind it is client-controlled, and trusting
    it would hand out a fresh IP window per fake address.
    """
    raw = headers.get("CF-Connecting-IP") or ""
    if raw:
        return raw.split(",")[0].strip()[:64]
    peer = peer or ""
    if peer.startswith("127.") or peer == "::1":
        forwarded = headers.get("X-Forwarded-For") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (peer or "unknown")[:64]
