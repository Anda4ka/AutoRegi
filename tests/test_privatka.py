"""Private-group upsell branches, 5/5 win, Basic line."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="privatka_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "test:token"
os.environ["AUTOREG_STATE_DIR"] = str(_TMP)
os.environ["USER_DATA_DIR"] = str(_TMP / "ud")
os.environ["USER_PROXY_DIR"] = str(_TMP / "up")
os.environ["UPSELL_BOT"] = "UpsellBot"

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import db as bot_db  # noqa: E402
from bot import privatka  # noqa: E402
from bot import config  # noqa: E402

config.DB_PATH = _TMP / "t.db"
bot_db.DB_PATH = _TMP / "t.db"
bot_db.init_db()


def _job(uid: int, *, ok: int, total: int, status: str = "done"):
    return type("J", (), {"user_id": uid, "ok": ok, "total": total, "status": status})()


def test_pay_url() -> None:
    assert privatka.pay_url() == "https://t.me/UpsellBot"


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(config, "UPSELL_BOT", "")
    assert not privatka.enabled()
    assert privatka.bot_username() == ""


def test_free_win_only_exact_5_of_5() -> None:
    uid = 880101
    bot_db.ensure_user(uid, "u", "U")
    assert privatka.is_free_win(_job(uid, ok=5, total=5))
    assert not privatka.is_free_win(_job(uid, ok=5, total=6))
    assert not privatka.is_free_win(_job(uid, ok=4, total=5))
    assert not privatka.is_free_win(_job(uid, ok=5, total=5, status="cancelled"))


def test_win_line_is_russian() -> None:
    line = privatka.win_line()
    assert "5/5 готово ✅" in line
    assert "приваткой" in line


def test_branch_vip_and_offer(monkeypatch) -> None:
    uid = 880102
    bot_db.ensure_user(uid, "u", "U")
    monkeypatch.setattr(privatka.bot_limits, "is_vip", lambda _u: True)
    assert privatka.branch(uid) == "vip"
    assert "VIP-доступ активен" in privatka.screen_text(uid)


def test_branch_file_sells_delta(monkeypatch) -> None:
    uid = 880103
    bot_db.ensure_user(uid, "u", "U")
    monkeypatch.setattr(privatka, "_in_group", lambda _u: False)
    monkeypatch.setattr(privatka, "_in_file", lambda _u: True)
    monkeypatch.setattr(privatka.bot_limits, "is_vip", lambda _u: False)
    monkeypatch.setattr(privatka.billing, "is_active", lambda _u: False)
    assert privatka.branch(uid) == "file"
    text = privatka.screen_text(uid)
    assert "Приватка" in text and "@UpsellBot" in text


def test_attach_after_job_adds_button() -> None:
    uid = 880104
    bot_db.ensure_user(uid, "u", "U")
    job = _job(uid, ok=5, total=5)
    text, kb = privatka.attach_after_job("done body", None, job)
    assert "5/5 готово" in text
    labels = [b.text for row in kb.keyboard for b in row]
    assert any("Приватка" in x for x in labels)


def test_log_funnel_event_writes_a_row() -> None:
    uid = 880105
    bot_db.ensure_user(uid, "u", "U")
    bot_db.log_funnel_event(uid, "priv_menu", "priv:menu")
    with bot_db.db() as conn:
        row = conn.execute(
            "SELECT user_id, event, branch, ts FROM funnel_events "
            "WHERE user_id=? AND event=?",
            (uid, "priv_menu"),
        ).fetchone()
    assert row is not None
    assert int(row["user_id"]) == uid
    assert row["event"] == "priv_menu"
    assert row["branch"] == "priv:menu"
    assert int(row["ts"]) > 0


def test_funnel_export_tsv_fills_zero_days_and_counts() -> None:
    from datetime import datetime, timezone, timedelta

    today = datetime.now(timezone.utc).date()
    noon = datetime(
        today.year, today.month, today.day, 12, 0, tzinfo=timezone.utc
    )
    ts = int(noon.timestamp())
    uid = 880106
    bot_db.ensure_user(uid, "u", "U")
    with bot_db.db() as conn:
        conn.execute("DELETE FROM funnel_events")
        conn.commit()
    with bot_db.db() as conn:
        conn.executemany(
            "INSERT INTO funnel_events (user_id, event, branch, ts) "
            "VALUES (?, ?, ?, ?)",
            [
                (uid, "win_shown", "", ts),
                (uid, "win_shown", "", ts),
                (uid, "priv_menu", "priv:menu", ts),
                (uid, "priv_cmd", "", ts),
                (uid, "priv_view", "offer", ts),
                (uid, "priv_view", "file", ts),
            ],
        )
        conn.commit()
    tsv = bot_db.funnel_export_tsv(2)
    lines = tsv.split("\n")
    assert len(lines) == 2, lines
    yesterday = (today - timedelta(days=1)).isoformat()
    zeros = "\t".join(["0"] * 8)
    assert lines[0] == f"{yesterday}\t{zeros}"
    last = lines[1].split("\t")
    assert last[0] == today.isoformat()
    # win_shown priv_menu priv_cmd view_offer view_file view_basic view_group view_vip
    assert last[1:] == ["2", "1", "1", "1", "1", "0", "0", "0"]
    try:
        bot_db.funnel_export_tsv(91)
        raise AssertionError("91 days must refuse")
    except ValueError:
        pass


def test_i18n_priv_keys_are_russian_in_both() -> None:
    from bot import i18n

    for key, vals in i18n.STRINGS.items():
        if key.startswith("priv."):
            assert vals["ru"].strip()
            assert vals["en"] == vals["ru"], key
