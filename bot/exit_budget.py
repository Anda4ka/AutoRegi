"""Per-exit request budgets for routes that rate-limit by IP.

Some endpoints count requests per source address and then refuse that address
for a long time. Rotating the proxy per row is not enough on its own: with a
few thousand requests drawn at random from a pool of a few thousand exits, the
birthday collisions alone put a fourth request on some of them. one route showed
exactly that — about 1% of rows lost, the share growing with the running total,
against a route that allows four per IP and was still refusing the same exit
90 seconds later.

Two rules fix it, and this module is where they live:

* **never spend more than `budget` requests on one exit** — set below whatever
  the route actually allows, since rejected requests count on its side too;
* **once an exit answers 429, take it out of rotation** for `cooldown`
  seconds; it will not recover before then.

Routes come in two kinds and `window` is what tells them apart. Some count for
good — one route files a single wallet per address and never forgets — and there the
budget is permanent: `window=0`. Others count inside a moving window and say so
("Try again in a few minutes"), and there a spend must be forgotten once the
window has passed, or the ledger slowly marks the whole pool spent over an
allowance the route reset hours ago.

**Why this is in `bot/` and not in the adapter.** `bot/registry.py` drops
`projects.<id>.*` from `sys.modules` on every `/reload` so that edits to
`api.py` apply. Ledger state kept in the adapter would reset with it, and an
exit already spent three times would be handed out three more — six against a
limit of four. Budgets registered here are keyed by name and survive, because
nothing purges `bot.*`.

**The ledger also outlives the process.** It used to live only in RAM, which
covered `/reload` and nothing else. Measured on a route that gives one
wallet per address and keeps that memory: 2026-08-19, ledger warm all day,
5841 OK / 57 FAIL (1%); after the restart of 2026-08-20 16:26 the same route
ran 339 OK / 92 FAIL (21%, and 44% over the last two hours), every one of them
`This IP address / device has already registered` — the bot walking back into
addresses it had itself spent the day before. So each ledger is mirrored to
``STATE_DIR/exit_budget/<name>.json`` and read back on the next start.

The mirror stores a short hash of each exit rather than its URL. The ledger
never needs to name an exit, only to recognise it again, and hashing keeps the
gateway credentials in `proxy.txt` out of a second file. Writes are debounced
(`SAVE_INTERVAL`) and atomic, so a busy route pays one small write a minute and
a crash costs at most that minute; nothing here may raise into a submission, so
every filesystem error is logged and swallowed.

The pool this was measured against is one rotating gateway whose 5000 lines
each hold a distinct session token; checked live, a line maps to a stable
egress address and different lines to different ones, so a ledger keyed by the
proxy URL is a ledger keyed by IP.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import random
import tempfile
import threading
import time
from pathlib import Path

from bot.config import STATE_DIR

# Beyond this many tracked exits the ledger is halved, cheapest-first.
LEDGER_MAX = 20000
# Seconds between mirrors of a ledger that keeps changing.
SAVE_INTERVAL = 30.0
LEDGER_DIR = Path(STATE_DIR) / "exit_budget"

_registry_lock = threading.Lock()
_registry: dict[str, "ExitBudget"] = {}

# url -> digest, computed once. `pick` hashes on every step of a scan over the
# whole pool, and the pool is thousands of lines long.
_keys: dict[str, str] = {}


def exit_key(proxy_url: str) -> str:
    """The ledger's name for an exit: short, stable, and not the credentials."""
    # Shared mutable cache read/written from every row thread — it stays
    # inside the registry lock like the rest of the module's shared state.
    with _registry_lock:
        key = _keys.get(proxy_url)
        if key is None:
            if len(_keys) > 4 * LEDGER_MAX:
                _keys.clear()
            key = hashlib.blake2s(proxy_url.encode("utf-8"), digest_size=8).hexdigest()
            _keys[proxy_url] = key
        return key


class ExitBudget:
    """A ledger of how much of each exit's allowance has been spent."""

    def __init__(
        self,
        name: str,
        *,
        budget: int = 3,
        cooldown: float = 3600.0,
        window: float = 0.0,
        path: Path | str | None = None,
    ):
        self.name = name
        self.budget = max(1, int(budget))
        self.cooldown = max(1.0, float(cooldown))
        # 0 = the route never forgets; otherwise how long a spend is remembered.
        self.window = max(0.0, float(window or 0.0))
        self._lock = threading.Lock()
        self._uses: dict[str, int] = {}
        self._used_at: dict[str, float] = {}
        self._banned: dict[str, float] = {}
        self._path = Path(path) if path is not None else LEDGER_DIR / f"{name}.json"
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    # ---------------------------------------------------------------- storage

    def _load(self) -> None:
        """Read the mirror, if there is one. A missing or broken file is not an
        error: an empty ledger is what the process had before this existed."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"[exits] {self.name}: cannot read {self._path}: {exc}", flush=True)
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[exits] {self.name}: unreadable ledger {self._path}: {exc}", flush=True)
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        uses = data.get("uses")
        used_at = data.get("used_at")
        used_at = used_at if isinstance(used_at, dict) else {}
        if isinstance(uses, dict):
            self._uses = {
                str(key): int(count)
                for key, count in uses.items()
                if isinstance(count, (int, float)) and count > 0
            }
            self._used_at = {
                str(key): float(when)
                for key, when in used_at.items()
                if isinstance(when, (int, float)) and str(key) in self._uses
            }
            if self.window:
                # A windowed allowance the route has already given back is not
                # ours to keep. Spends with no timestamp are older than this
                # field and count as expired.
                self._uses = {
                    key: count
                    for key, count in self._uses.items()
                    if now - self._used_at.get(key, 0.0) < self.window
                }
                self._used_at = {
                    key: when for key, when in self._used_at.items() if key in self._uses
                }
        banned = data.get("banned")
        if isinstance(banned, dict):
            # A bench outlives the restart only for what is left of its cooldown.
            self._banned = {
                str(key): float(when)
                for key, when in banned.items()
                if isinstance(when, (int, float)) and 0 <= now - float(when) < self.cooldown
            }
        print(
            f"[exits] {self.name}: resumed {len(self._uses)} spent, "
            f"{len(self._banned)} benched, from {self._path}",
            flush=True,
        )

    def _snapshot_locked(self, now: float, *, force: bool = False) -> dict | None:
        """The payload to write, or None when nothing is due. Call under lock."""
        if not self._dirty:
            return None
        if not force and now - self._saved_at < SAVE_INTERVAL:
            return None
        self._dirty = False
        self._saved_at = now
        return {
            "name": self.name,
            "budget": self.budget,
            "cooldown": self.cooldown,
            "saved_at": now,
            "window": self.window,
            "uses": dict(self._uses),
            "used_at": dict(self._used_at),
            "banned": dict(self._banned),
        }

    def _write(self, payload: dict | None) -> None:
        """Mirror the ledger. Never raises: a row must not fail over the disk."""
        if payload is None:
            return
        tmp_path = ""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=f".{self.name}.", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            # mkstemp already made it 0600, and replace keeps the mode.
            os.replace(tmp_path, self._path)
            tmp_path = ""
        except Exception as exc:  # noqa: BLE001 — disk shapes vary, none are fatal
            print(f"[exits] {self.name}: cannot write {self._path}: {exc}", flush=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def flush(self) -> None:
        """Mirror now, whatever the debounce says."""
        with self._lock:
            payload = self._snapshot_locked(time.time(), force=True)
        self._write(payload)

    # ----------------------------------------------------------------- ledger

    def _prune_locked(self, now: float) -> None:
        for key, when in list(self._banned.items()):
            if now - when >= self.cooldown:
                del self._banned[key]
                self._dirty = True
        if self.window:
            for key, when in list(self._used_at.items()):
                if now - when >= self.window:
                    del self._used_at[key]
                    self._uses.pop(key, None)
                    self._dirty = True
        if len(self._uses) > LEDGER_MAX:
            # Drop the least-spent exits: those are the ones `pick` would hand
            # out again anyway, so forgetting them costs nothing.
            for key, _n in sorted(self._uses.items(), key=lambda kv: kv[1])[: LEDGER_MAX // 2]:
                self._uses.pop(key, None)
                self._used_at.pop(key, None)
            self._dirty = True

    def pick(self, proxies: list[str], proxy_idx: int, attempt: int = 0) -> str | None:
        """An exit with allowance left, scanning from this row's own offset.

        Falls back to the least-spent exit rather than returning nothing: a
        request that might be refused still beats no request, and the caller
        treats the refusal as retryable. Returns ``None`` only for an empty
        pool.
        """
        if not proxies:
            return None
        total = len(proxies)
        now = time.time()
        # A retry starts somewhere else entirely, so a row that was just
        # refused does not walk into the neighbours of a burnt exit.
        start = proxy_idx % total if attempt == 0 else random.randint(0, total - 1)
        chosen = proxies[proxy_idx % total]
        with self._lock:
            self._prune_locked(now)
            cheapest: tuple[int, str, str] | None = None
            for step in range(total):
                url = proxies[(start + step) % total]
                key = exit_key(url)
                banned_at = self._banned.get(key)
                if banned_at is not None and now - banned_at < self.cooldown:
                    continue
                used = self._uses.get(key, 0)
                if used < self.budget:
                    self._uses[key] = used + 1
                    self._used_at[key] = now
                    self._dirty = True
                    chosen = url
                    break
                if cheapest is None or used < cheapest[0]:
                    cheapest = (used, key, url)
            else:
                if cheapest is not None:
                    self._uses[cheapest[1]] = cheapest[0] + 1
                    self._used_at[cheapest[1]] = now
                    self._dirty = True
                    chosen = cheapest[2]
            payload = self._snapshot_locked(now)
        self._write(payload)
        return chosen

    def penalise(self, proxy_url: str | None) -> None:
        """This exit answered 429 — retire it for the cooldown."""
        if not proxy_url:
            return
        now = time.time()
        with self._lock:
            key = exit_key(proxy_url)
            self._banned[key] = now
            self._uses[key] = self.budget
            self._used_at[key] = now
            self._dirty = True
            payload = self._snapshot_locked(now)
        self._write(payload)

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "budget": self.budget,
                "window": self.window,
                "tracked": len(self._uses),
                "banned": len(self._banned),
                "path": str(self._path),
            }

    def reset(self) -> None:
        """Tests only — production state is meant to outlive a reload."""
        with self._lock:
            self._uses.clear()
            self._used_at.clear()
            self._banned.clear()
            self._dirty = False
            self._saved_at = 0.0
        try:
            self._path.unlink()
        except OSError:
            pass


def get(
    name: str,
    *,
    budget: int = 3,
    cooldown: float = 3600.0,
    window: float = 0.0,
    path: Path | str | None = None,
) -> ExitBudget:
    """The ledger for `name`, created once per process.

    Re-importing a project module returns the same instance, which is the whole
    point: `/reload` must not hand back allowance the route has already spent.
    The first call reads the mirror from disk, so a restart does not either.
    """
    with _registry_lock:
        found = _registry.get(name)
        if found is None:
            found = ExitBudget(
                name, budget=budget, cooldown=cooldown, window=window, path=path
            )
            _registry[name] = found
        return found


def flush_all() -> None:
    """Mirror every ledger — on shutdown, so the last minute is not lost."""
    with _registry_lock:
        ledgers = list(_registry.values())
    for ledger in ledgers:
        ledger.flush()


atexit.register(flush_all)
