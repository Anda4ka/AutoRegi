"""NFT Trencher feed: sync into the calendar, registry matching, T-1h reminders.

All offline: fetch is stubbed, the audience is seeded, broadcast.enqueue is
captured. Modules are reached through ``nftt``'s own references — another
suite test purges ``bot.*`` from ``sys.modules`` and re-imports them, which
leaves two live copies of ``config``/``db``; patching the one in ``sys.modules``
while ``nftt`` holds the other silently tests nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot import nftt


def _mint(**kw):
    base = {
        "id": "robinhood:0x1f00dead",
        "chain": "robinhood",
        "contract": "0x1f00dead",
        "name": "Feed Demo",
        "slug": "feed-demo",
        "links": {"x": "https://x.com/feed-demo",
                  "site": "https://example-feed.com/",
                  "opensea": "https://opensea.io/collection/feed-demo",
                  "mint": "https://example-feed.com/"},
        "stages": [
            {"kind": "gtd", "label": "GTD", "public": False,
             "open_to_visitors": True,
             "start": "2026-09-12T16:00:00Z", "end": "2026-09-12T17:00:00Z",
             "state": "upcoming", "price": 0.0, "currency": "ETH",
             "max_per_wallet": 2},
            {"kind": "public", "label": "Public stage", "public": True,
             "open_to_visitors": True,
             "start": "2026-09-12T18:00:00Z", "end": None,
             "state": "upcoming", "price": 0.002, "currency": "ETH",
             "max_per_wallet": 5},
            {"kind": "team", "label": "Team", "public": False,
             "open_to_visitors": False,
             "start": "2026-09-12T10:00:00Z", "end": None,
             "state": "upcoming", "price": 0.0, "currency": "ETH",
             "max_per_wallet": 1},
        ],
        "status": "upcoming", "minted": 0, "supply": 3333,
        "tier": "warm", "flags": [], "updated_at": "2026-09-10T00:00:00Z",
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _clean_calendar():
    yield
    for row in nftt.bot_mints.calendar():
        nftt.bot_mints.clear_project(row["project_id"])


def test_sync_writes_calendar_and_skips_closed():
    written = nftt.sync_to_calendar([_mint(), _mint(status="ended", slug="bye")])
    assert written == 1  # ended rows are not imported
    rows = {r["project_id"]: r for r in nftt.bot_mints.calendar()}
    entry = rows["nftt_feed-demo"]
    assert entry["project"] == "(NoReg) Feed Demo"
    assert entry["supply"] == 3333
    kinds = {p["kind"]: p for p in entry["phases"]}
    assert "gtd" in kinds and "public" in kinds
    # team/dev/reserve windows are not a reader's business
    assert "team" not in kinds
    assert kinds["gtd"]["price"] == "0.0 ETH"
    assert "лимит 2/кошелёк" in kinds["gtd"]["note"]
    assert kinds["gtd"]["starts_at"] == "2026-09-12T16:00:00+00:00"


def test_registry_match_drops_noreg(monkeypatch):
    monkeypatch.setattr(
        nftt, "_registry_link_index", lambda: {"x.com/feed-demo": "example"})
    nftt.sync_to_calendar([_mint()])
    rows = {r["project_id"]: r for r in nftt.bot_mints.calendar()}
    assert "nftt_feed-demo" not in rows
    assert "example" in rows  # attached to the live project, no (NoReg) prefix


def test_reminder_window_and_fire(monkeypatch):
    monkeypatch.setattr(nftt.config, "MINTS_ENABLED", True)
    start = datetime.now(timezone.utc) + timedelta(minutes=60)
    nftt.sync_to_calendar([_mint(stages=[dict(_mint()["stages"][0],
                                              start=start.isoformat())])])
    audience = [1, 2]
    monkeypatch.setattr(nftt.bot_db, "mint_remind_user_ids",
                        lambda: audience)
    sent_batches = []
    import bot.broadcast as broadcast_mod

    monkeypatch.setattr(broadcast_mod, "enqueue",
                        lambda batch, ids, **kw: sent_batches.append(
                            (batch, list(ids), kw)))
    fired = nftt.fire_reminders()
    assert fired == 1
    batch, ids, kw = sent_batches[0]
    assert ids == audience and batch.startswith("mintrem-")
    assert "Feed Demo" in kw["body"] and "UTC" in kw["body"]

    # marker holds: a second sweep (restart, same minute) does not re-blast
    assert nftt.fire_reminders() == 0
    assert len(sent_batches) == 1

    # a phase outside the window is not reminded
    early = _mint(slug="far", stages=[dict(_mint()["stages"][0],
                                           start=(datetime.now(timezone.utc)
                                                  + timedelta(hours=5)).isoformat())])
    nftt.sync_to_calendar([early])
    assert nftt.fire_reminders() == 0


def test_feed_disabled_without_key(monkeypatch):
    monkeypatch.setattr(nftt.config, "MINTS_ENABLED", False)
    assert nftt.feed_enabled() is False
    assert nftt.maybe_sync() == -1
    monkeypatch.setattr(nftt.config, "MINTS_ENABLED", True)
    monkeypatch.setattr(nftt.config, "NFTT_API_KEY", "")
    assert nftt.feed_enabled() is False
    # Reminders depend on the calendar switch, not on the feed key: with the
    # calendar on and nobody opted in, the sweep is a quiet zero.
    assert nftt.fire_reminders() == 0


def test_fetch_401_is_distinct_from_empty(monkeypatch):
    class _Resp:
        status_code = 401
        text = "nope"

    monkeypatch.setattr(nftt.config, "MINTS_ENABLED", True)
    monkeypatch.setattr(nftt.config, "NFTT_API_KEY", "nftt_x")
    monkeypatch.setattr(nftt.requests, "get", lambda *a, **kw: _Resp())
    assert nftt.fetch_mints() is None
    # None (dead feed) must never be confused with [] (empty board)
    assert nftt.sync_to_calendar(None) == -1
