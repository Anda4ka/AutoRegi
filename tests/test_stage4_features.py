"""Stage-4 features: /data row delete, registration export, proxy health."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="stage4_"))
_DB = _TMP / "t.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")
os.environ["JOB_PAYLOAD_DIR"] = str(_TMP / "job_payloads")
os.environ["BOT_RESULTS"] = str(_TMP / "results.jsonl")

for _mod in list(sys.modules):
    if (
        _mod == "bot"
        or _mod.startswith("bot.")
        or _mod == "tgbot"
        or _mod == "handlers"
        or _mod.startswith("handlers.")
    ):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import user_data  # noqa: E402
from bot import submit_queue as sq  # noqa: E402

bot_db.DB_PATH = str(_DB)  # type: ignore[attr-defined]
config.JOB_PAYLOAD_DIR = _TMP / "job_payloads"
bot_db.init_db()
sq.init_schema()

UID = 920001
W = ["0x" + f"{i:040x}" for i in range(1, 5)]  # four wallets


def _write_data():
    bot_db.ensure_user(UID, "u", "U")
    path = user_data.user_data_path(UID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"h{i}|{w}" for i, w in enumerate(W, 1)) + "\n",
        encoding="utf-8",
    )


def test_delete_row_removes_exactly_that_row() -> None:
    _write_data()
    assert user_data.delete_user_data_row(UID, 2)
    rows = user_data.load_user_data_rows(UID)
    assert [r.wallet for r in rows] == [W[0], W[2], W[3]]
    # the cache noticed the rewrite (same file identity, new stamp)
    assert user_data.count_user_data(UID) == 3


def test_delete_row_bounds() -> None:
    _write_data()
    assert not user_data.delete_user_data_row(UID, 0)
    assert not user_data.delete_user_data_row(UID, 99)
    assert user_data.count_user_data(UID) == 4


def test_registrations_export_query() -> None:
    bot_db.record_submission(UID, "demop", "h1", W[0], "OK", "")
    bot_db.record_submission(UID, "demop", "h2", W[1], "ALREADY", "")
    bot_db.record_submission(UID, "demop", "h3", W[2], "FAIL", "proxy error via http://1.2.3.4:8080")
    other = UID + 1
    bot_db.ensure_user(other, None, None)
    bot_db.record_submission(other, "demop", "x", W[3], "OK", "")
    rows = bot_db.user_project_registrations(UID, "demop")
    # only this user, only OK/ALREADY, newest first
    assert sorted(r["wallet"] for r in rows) == [W[0], W[1]]
    assert not bot_db.user_project_registrations(UID, "nothing")


def test_proxy_health_counts_transport_failures() -> None:
    h = bot_db.user_proxy_health(UID)
    # at least the FAIL from the previous test counts as a row
    assert h["rows"] >= 3
    assert h["fails"] >= 1
    # "proxy error via …" is one of _PROXY_FAIL_PREFIXES → counted as proxy
    assert h["fails_proxy"] >= 1


def test_i18n_keys_exist_both_languages() -> None:
    from bot.i18n import STRINGS

    for key in (
        "btn.data_edit",
        "data.edit_title",
        "data.del_confirm",
        "toast.data_row_deleted",
        "toast.data_bad_row",
        "btn.export_results",
        "toast.no_results",
        "export.caption",
        "proxy.health",
    ):
        assert key in STRINGS, key
        assert "ru" in STRINGS[key] and "en" in STRINGS[key], key


def test_user_stats_daily_buckets() -> None:
    from datetime import datetime, timedelta, timezone

    uid = 920777
    bot_db.ensure_user(uid, None, None)
    now = datetime.now(timezone.utc)
    # today: 2 OK + 1 FAIL; yesterday (within 7d): 1 OK
    today = now.isoformat()
    yest = (now - timedelta(days=1)).isoformat()
    for ts, st in ((today, "OK"), (today, "OK"), (today, "FAIL"), (yest, "OK")):
        with bot_db.db() as conn:
            conn.execute(
                "INSERT INTO submissions (user_id, project_id, handle, wallet,"
                " status, detail, created_at) VALUES (?,'demop','h', ?, ?, '', ?)",
                (uid, f"0x{abs(hash(ts + st)) % 16**40:040x}", st, ts),
            )
            conn.commit()
    s = bot_db.user_stats_daily(uid, days=7)
    assert s["days"] == 7
    assert s["totals"]["ok"] == 3 and s["totals"]["fail"] == 1
    day_keys = sorted(s["by_day"])
    assert len(day_keys) == 2
    assert s["by_project"][0]["project_id"] == "demop"
    assert s["by_project"][0]["ok"] == 3
