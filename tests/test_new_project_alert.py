"""New-project alert: seed once, offer once, never blast a backlog.

The danger of this feature is not that it misses a project — it is that it
announces sixteen at once, or announces the same one twice. So:

  * the very first run records what already exists and stays silent;
  * a genuinely new id is returned exactly once, and never again after it has
    been marked (whether the admin sent it or declined);
  * the marker outranks the known-ids list, so a project removed and re-added
    cannot re-blast;
  * with alerts switched off, ids are absorbed rather than queued up;
  * the announcement itself is written in each recipient's own language.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="newproj_"))
_DB = _TMP / "t.db"

os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "t:t"
os.environ["SUBSCRIBERS_FILE"] = str(_TMP / "active_subscribers.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["LIMITS_CACHE_TTL"] = "0"
os.environ["BROADCAST_MIN_INTERVAL"] = "0"

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

from bot import new_project_alert as npa  # noqa: E402


def _ok(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


class FakeProject:
    def __init__(self, pid: str = "newthing") -> None:
        self.id = pid
        self.name = "New Thing"
        self.ok_limit = 30
        self.link = "https://newthing.xyz/"
        self.twitter_url = "https://x.com/newthing"
        self.mint_info = "дата TBA · 10 000 шт"
        self.mint_info_en = "date TBA · 10 000 pcs"


def _reset() -> None:
    bot_db.init_db()
    with bot_db.db() as conn:
        conn.execute("DELETE FROM bot_settings WHERE key LIKE 'alert_new_project%'")
        conn.execute("DELETE FROM bot_settings WHERE key = 'known_project_ids'")
        conn.execute("DELETE FROM bot_settings WHERE key = 'new_project_alerts_off'")
        conn.execute("DELETE FROM broadcast_outbox")
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM users")
        conn.commit()


def test_first_run_seeds_and_stays_silent() -> None:
    _reset()
    existing = ["alpha", "beta", "gamma"]
    _ok(npa.detect_new(existing) == [], "the seeding run must announce nothing")
    _ok(npa._known_ids() == set(existing), npa._known_ids())
    # A second pass over the same registry is still quiet.
    _ok(npa.detect_new(existing) == [])


def test_new_id_is_offered_once() -> None:
    _reset()
    npa.detect_new(["alpha"])
    _ok(npa.detect_new(["alpha", "beta"]) == ["beta"])
    # Still unmarked → still offered, so a failed preview is not lost.
    _ok(npa.detect_new(["alpha", "beta"]) == ["beta"])
    npa.mark_announced("beta")
    _ok(npa.detect_new(["alpha", "beta"]) == [], "a marked id must never return")


def test_marker_survives_a_remove_and_readd() -> None:
    _reset()
    npa.detect_new(["alpha"])
    npa.mark_announced("beta")
    # «beta» dropped out of the registry and came back: the marker is the last
    # word, so the whole base is not told about it twice.
    _ok(npa.detect_new(["alpha"]) == [])
    _ok(npa.detect_new(["alpha", "beta"]) == [])


def test_disabled_switch_absorbs_ids() -> None:
    _reset()
    npa.detect_new(["alpha"])
    npa.set_enabled(False)
    _ok(not npa.is_enabled())
    fresh = npa.detect_new(["alpha", "beta"])
    _ok(fresh == ["beta"], "detection still reports; the caller decides")
    for pid in fresh:
        npa.mark_announced(pid)
    npa.set_enabled(True)
    _ok(npa.is_enabled())
    _ok(npa.detect_new(["alpha", "beta"]) == [], "no backlog after switching on")


def test_env_kill_switch() -> None:
    _reset()
    os.environ["NEW_PROJECT_ALERTS"] = "0"
    try:
        _ok(not npa.is_enabled(), "env must be able to shut this off entirely")
    finally:
        os.environ.pop("NEW_PROJECT_ALERTS", None)
    _ok(npa.is_enabled())


def test_render_is_localized() -> None:
    project = FakeProject()
    ru = npa.render(project, "ru")
    en = npa.render(project, "en")
    _ok("New Thing" in ru and "New Thing" in en)
    _ok("10 000 шт" in ru, ru)
    _ok("10 000 pcs" in en, en)
    _ok("Минт" in ru and "Mint" in en)
    _ok("newthing.xyz" in ru and "@newthing" in ru, ru)
    _ok("Лимит:" in ru and "Limit:" in en, ru)
    _ok("подписчикам" in ru and "subscribers" in en, ru)


def test_announce_queues_one_row_per_active_user_in_their_language() -> None:
    _reset()
    for uid in (1, 2, 3):
        bot_db.ensure_user(uid, f"u{uid}", f"U{uid}")
    bot_db.set_lang(2, "en")
    # 1 and 2 submitted recently; 3 never did and must not be in the audience.
    bot_db.record_submission(1, "alpha", "h1", "0x1", "OK", "ok")
    bot_db.record_submission(2, "alpha", "h2", "0x2", "OK", "ok")

    project = FakeProject()
    batch, queued = npa.announce(project)
    _ok(batch == "newproj:newthing", batch)
    _ok(queued == 2, f"queued {queued} — audience must be recent submitters only")

    rows = {r["user_id"]: r for r in bot_db.next_broadcast_rows(10)}
    _ok(set(rows) == {1, 2}, sorted(rows))
    _ok("Минт" in rows[1]["body"], rows[1]["body"])
    _ok("Mint" in rows[2]["body"], rows[2]["body"])
    _ok(rows[1]["interval_sec"] == npa.DEFAULT_INTERVAL_SEC, rows[1]["interval_sec"])
    _ok("project:newthing" in rows[1]["markup_json"], rows[1]["markup_json"])
    # Re-approving the same project cannot double-send.
    _ok(npa.announce(project)[1] == 0, "second announce must insert nothing")


def test_preview_states_the_pace() -> None:
    text = npa.preview_text(FakeProject(), recipients=130)
    _ok("130" in text, text)
    _ok("RU" in text and "EN" in text, text)
    _ok("VIP" in text, text)


def main() -> None:
    tests = [
        test_first_run_seeds_and_stays_silent,
        test_new_id_is_offered_once,
        test_marker_survives_a_remove_and_readd,
        test_disabled_switch_absorbs_ids,
        test_env_kill_switch,
        test_render_is_localized,
        test_announce_queues_one_row_per_active_user_in_their_language,
        test_preview_states_the_pace,
    ]
    failed = 0
    for tfn in tests:
        try:
            tfn()
            print(f"PASS {tfn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {tfn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
