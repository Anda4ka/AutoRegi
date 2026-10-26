"""How many of a user's wallets a project has not seen yet.

The screen used to answer "what will this send" with `min(wallets, free slots)`,
which is the number *before* dedup. On a mature account that is a lie with
consequences: measured 2026-08-17 on the operator's own list, 18 of 21 projects
had every one of 695 wallets already registered, so eighteen «Старт» buttons
were offered and eighteen would have refused. A button that refuses teaches the
user that the screen is decoration.

The honest number costs a query per project — about 7 ms against 695 wallets,
146 ms across a full list, which is more than the whole rest of a screen
refresh. So it is cached, and the cache is keyed on the two things that can
change the answer:

* the user's accounts file, by ``(mtime_ns, size)`` — a new list, new answer;
* the project's generation from ``bot.db``, bumped whenever a settled row lands
  on it, by anyone. Dedup is global: someone else's registration takes a wallet
  out of play here too.

Both keys are facts rather than clocks, so there is no staleness window to
reason about — an entry is either about the current world or it is discarded.
"""

from __future__ import annotations

import threading

from bot import db as bot_db
from bot import user_data as user_data_mod

# (user_id, project_id) -> (data_stamp, project_generation, fresh_count)
_cache: dict[tuple[int, str], tuple[tuple[int, int], int, int]] = {}
_lock = threading.Lock()
_CACHE_MAX = 4096


def _data_stamp(user_id: int) -> tuple[int, int]:
    """Identity of this user's accounts file, or (0, 0) when there is none."""
    try:
        stat = user_data_mod.user_data_path(user_id).stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def fresh_count(
    user_id: int,
    project_id: str,
    wallets: list[str] | None = None,
    *,
    wallet_set: set[str] | None = None,
) -> int:
    """Wallets of this user that are still unregistered on this project.

    ``wallets`` is accepted so a caller walking every project reads the file
    once instead of twenty times; omitted, it is loaded here. ``wallet_set`` is
    the same list, lowercased, for a caller that already built it — see below
    for why this path is worth an argument.
    """
    uid = int(user_id)
    pid = (project_id or "").strip()
    if not pid:
        return 0
    stamp = _data_stamp(uid)
    generation = bot_db.project_generation(pid)
    key = (uid, pid)

    with _lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] == stamp:
        if hit[1] == generation:
            return hit[2]
        # The generation moved, which used to end the story. But it is bumped
        # by *anyone's* settled row, and dedup being global does not make it
        # everyone's business: a registration only changes this user's answer
        # if the wallet was one of theirs. While the queue is busy the counter
        # ticks every few seconds, so keying on it alone threw every user's
        # cached answer away exactly when the bot had least to spare — the
        # recomputation is a query per project, on every four-second poll.
        #
        # So ask which wallets moved. `None` means the trail no longer reaches
        # back that far and we have to recompute anyway.
        settled = bot_db.project_settled_since(pid, hit[1])
        if settled is not None:
            mine = wallet_set if wallet_set is not None else {
                (w or "").strip().lower()
                for w in (wallets if wallets is not None else [
                    r.wallet for r in user_data_mod.load_user_data_rows(uid)
                ])
            }
            if not (settled & mine):
                with _lock:
                    _cache[key] = (stamp, generation, hit[2])
                return hit[2]

    rows = wallets if wallets is not None else [
        r.wallet for r in user_data_mod.load_user_data_rows(uid)
    ]
    if not rows:
        count = 0
    else:
        known = bot_db.wallets_already_registered(pid, rows)
        count = sum(
            1 for w in rows if (w or "").strip().lower() not in known
        )

    with _lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (stamp, generation, count)
    return count


def invalidate(user_id: int | None = None) -> None:
    """Drop cached answers; mostly for tests and for an explicit /reload."""
    with _lock:
        if user_id is None:
            _cache.clear()
            return
        uid = int(user_id)
        for key in [k for k in _cache if k[0] == uid]:
            del _cache[key]
