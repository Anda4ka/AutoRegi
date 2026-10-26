"""Rendering tests for the UX-first /start, project screen and queue progress.

Covers the «first release» UX changes:
  * compact contextual /start (state + health legend, no project list duplicate)
  * project screen (will-send / slots / requirements / human buttons)
  * queue progress: project NAMES (not ids) + progress bar + human status terms
  * two-step cancel callback contract (qcancel:ask|yes|no)
  * pluralization + first-visit auto-language detection helper.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ux_render_test_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"
os.environ["PROXY_FILE"] = str(_TMP / "proxy.txt")
# Redirect per-user storage to the temp dir so the tests NEVER touch the real
# production data/user_data or data/user_proxies directories.
os.environ["USER_DATA_DIR"] = str(_TMP / "user_data")
os.environ["USER_PROXY_DIR"] = str(_TMP / "user_proxies")

# `tgbot` is purged with `bot.*` on purpose. Left cached, it keeps the bot
# modules dropped here — so this file's writes land in one database while
# tgbot reads another, and the mismatch only shows up in whole-suite runs.
for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot.") or _mod == "tgbot" or _mod == "handlers" or _mod.startswith("handlers."):
        del sys.modules[_mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import i18n  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import submit_queue as sq  # noqa: E402
from bot import ui_screens  # noqa: E402
from bot import user_data as ud  # noqa: E402
from bot.ux import progress_bar  # noqa: E402

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]

import pytest  # noqa: E402

import tgbot  # noqa: E402
from bot import proxy_pool  # noqa: E402

from bot.registry import get_project, list_projects, load_projects  # noqa: E402

load_projects()


def _set_proxies(pool) -> None:
    """Set the shared proxy pool deterministically for ONE test.

    Each test sets the pool it needs instead of mutating a module global, so
    the suite is order-independent and pytest-safe (no leakage between modules
    that share the imported ``tgbot``)."""
    proxy_pool._proxies_loaded = True
    proxy_pool._proxies[:] = list(pool)


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def _seed_user(uid: int, first: str = "U", n_accounts: int = 0) -> None:
    bot_db.ensure_user(uid, "u", first)
    if n_accounts > 0:
        data = "\n".join(
            f"u{i}|0x" + ("%02x" % (i + 1)) * 20 for i in range(n_accounts)
        )
        ud.save_user_data_from_bytes(uid, data.encode("utf-8"))
    else:
        ud.clear_user_data(uid)


def _first_btn_text(kb) -> str:
    return kb.keyboard[0][0].text


def _btn_callback(kb, needle: str) -> str | None:
    for row in kb.keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith(needle):
                return b.callback_data
    return None


def test_progress_bar() -> None:
    _assert(progress_bar(0, 10) == "░" * 10)
    _assert(progress_bar(10, 10) == "█" * 10)
    _assert(progress_bar(5, 10) == "█" * 5 + "░" * 5)
    _assert(progress_bar(14, 24) == "██████░░░░")
    _assert(progress_bar(0, 0) == "░" * 10)  # total=0 safe


def test_plurals() -> None:
    _assert(i18n.noun_accounts(1, "ru") == "аккаунт")
    _assert(i18n.noun_accounts(2, "ru") == "аккаунта")
    _assert(i18n.noun_accounts(5, "ru") == "аккаунтов")
    _assert(i18n.noun_accounts(11, "ru") == "аккаунтов")
    _assert(i18n.noun_accounts(1, "en") == "account")
    _assert(i18n.noun_accounts(2, "en") == "accounts")
    _assert(i18n.noun_registrations(1, "ru", accusative=True) == "регистрацию")
    _assert(i18n.noun_registrations(3, "ru", accusative=True) == "регистрации")
    _assert(i18n.noun_registrations(24, "ru", accusative=True) == "регистрации")
    _assert(i18n.noun_registrations(25, "ru", accusative=True) == "регистраций")
    _assert(i18n.noun_registrations(11, "ru", accusative=True) == "регистраций")


def test_welcome_contextual_no_data() -> None:
    bot_db.init_db()
    sq.init_schema()
    uid = 10001
    _seed_user(uid, n_accounts=0)
    txt = tgbot.welcome_text("Андрей", load_projects(), user_id=uid, lang="ru")
    # State-driven, not a project list dump
    _assert("Привет, Андрей!" in txt)
    _assert("Аккаунты:" in txt and "не загружены" in txt)
    _assert("Проект:" in txt and "не выбран" in txt)
    _assert("Очередь:" in txt and "пусто" in txt)
    _assert("Лимит:" in txt)
    _assert("🟢 открыта" in txt and "⚪ не проверена" in txt)
    # Must NOT list every project as bullets anymore
    _assert("• " not in txt)
    # Projects stay visible (direct/bulk flows), but the upload CTA leads.
    kb = tgbot.projects_keyboard("ru", user_id=uid)
    _assert(_first_btn_text(kb) == "📤 Загрузить аккаунты")
    _assert(_btn_callback(kb, "data:menu") == "data:menu")
    # Project pick buttons are present (no longer hidden when data is missing).
    _assert(_btn_callback(kb, "project:") is not None)


def test_welcome_contextual_with_data() -> None:
    uid = 10002
    _seed_user(uid, n_accounts=24)
    txt = tgbot.welcome_text("Bob", load_projects(), user_id=uid, lang="ru")
    _assert("Аккаунты:" in txt and "24" in txt)
    # Keyboard shows real project buttons
    kb = tgbot.projects_keyboard("ru", user_id=uid)
    _assert(_btn_callback(kb, "project:") is not None)
    _assert(_first_btn_text(kb) != "📤 Загрузить аккаунты")


def _project_buttons(kb) -> list:
    return [
        b
        for row in kb.keyboard
        for b in row
        if (b.callback_data or "").startswith("project:")
    ]


def _all_buttons(kb) -> list:
    return [b for row in kb.keyboard for b in row]


def test_projects_keyboard_paginates_and_hides_locked() -> None:
    # pytest.MonkeyPatch.context() instead of the monkeypatch fixture so the
    # legacy `python -m tests.test_ux_render` runner (no fixtures) works too.
    with pytest.MonkeyPatch.context() as monkeypatch:
        _run_projects_keyboard_paginates(monkeypatch)


def _run_projects_keyboard_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = 10003
    _seed_user(uid, n_accounts=5)
    # Pagination must not depend on how many projects happen to be enabled:
    # a small page keeps the nav assertions meaningful as the set grows.
    monkeypatch.setattr(ui_screens, "_projects_per_page", lambda: 3)
    monkeypatch.setattr(tgbot, "_projects_per_page", lambda: 3)
    # Fill one project up to the free limit: it must leave the main list.
    proj = get_project("example")
    if proj is not None:
        lim = bot_limits.effective_ok_limit(uid)
        for i in range(lim):
            bot_db.record_submission(uid, proj.id, "h%d" % i, "0x" + ("%02x" % i) * 40, "OK", "")

    kb = tgbot.projects_keyboard("ru", user_id=uid)
    project_rows = [
        row
        for row in kb.keyboard
        if row and (row[0].callback_data or "").startswith("project:")
    ]
    if len(project_rows) >= 2 or any(len(row) == 2 for row in project_rows):
        _assert(any(len(row) == 2 for row in project_rows), "expected two-column rows")
    _assert(all(len(row) <= 2 for row in project_rows), "max two projects per row")

    per_page = tgbot._projects_per_page()
    buttons = _project_buttons(kb)
    _assert(len(buttons) <= per_page, f"one page only, got {len(buttons)}")
    labels = [b.text for b in buttons]
    _assert(not any(l.startswith("🔒") for l in labels), f"locked hidden: {labels}")

    callbacks = [b.callback_data or "" for b in _all_buttons(kb)]
    _assert("projects:locked:1" in callbacks, f"locked toggle offered: {callbacks}")
    _assert(
        all(len(c.encode("utf-8")) <= 64 for c in callbacks),
        "callback_data must stay under Telegram's 64-byte cap",
    )

    if len(list_projects()) > per_page:
        pages = [c for c in callbacks if c.startswith("projects:page:")]
        _assert(len(pages) == 2, f"prev/next offered: {pages}")
        second = tgbot.projects_keyboard("ru", user_id=uid, page=1)
        first_ids = {b.callback_data for b in _project_buttons(kb)}
        second_ids = {b.callback_data for b in _project_buttons(second)}
        _assert(first_ids != second_ids, "page 2 shows different projects")
        _assert(not (first_ids & second_ids), "pages must not overlap")


def test_projects_keyboard_locked_view() -> None:
    uid = 10013
    _seed_user(uid, n_accounts=5)
    proj = get_project("example")
    if proj is None:
        return
    lim = bot_limits.effective_ok_limit(uid)
    for i in range(lim):
        bot_db.record_submission(uid, proj.id, "L%d" % i, "0x" + ("%02x" % (i + 100)) * 20, "OK", "")

    kb = tgbot.projects_keyboard("ru", user_id=uid, show_locked=True)
    labels = [b.text for b in _project_buttons(kb)]
    _assert(labels, "locked view lists the completed projects")
    _assert(all(l.startswith("🔒") for l in labels), f"only locked here: {labels}")
    callbacks = [b.callback_data or "" for b in _all_buttons(kb)]
    _assert("projects:locked:0" in callbacks, "way back to the main list")


def test_projects_with_work_left_come_first() -> None:
    """Work outranks a confirmed health check, and that order is the point.

    `is_open` only separates "checked and reachable" from "not checked yet" —
    genuinely broken projects are excluded before sorting. Ranking health first
    put a finished project above an unchecked one that still had wallets
    waiting, which is backwards: after a restart nothing has been checked yet.
    """
    projects = list_projects()
    if len(projects) < 3:
        return
    started_open, untouched_unknown, untouched_open = projects[0], projects[1], projects[2]
    ranked = sorted(
        [(started_open, 5, "open"), (untouched_unknown, 0, None), (untouched_open, 0, "open")],
        key=lambda item: tgbot.project_sort_key(item[0], item[1], item[2]),
    )
    _assert(ranked[0][0] is untouched_open, "untouched and open leads")
    _assert(ranked[1][0] is untouched_unknown, "untouched beats a checked-but-finished one")
    _assert(ranked[2][0] is started_open, "nothing left to do sinks")


def test_a_project_with_nothing_left_sinks_below_one_with_work() -> None:
    """`fresh` is the honest measure: slots left is not work left.

    A user whose every wallet is already registered on a project still has free
    slots there, so the old `ok_n <= 0` test called it untouched-ish and sorted
    it high. What decides is how many wallets the project has not seen.
    """
    projects = list_projects()
    if len(projects) < 2:
        return
    done, has_work = projects[0], projects[1]
    ranked = sorted(
        [(done, 0, "open", 0), (has_work, 900, "open", 12)],
        key=lambda item: tgbot.project_sort_key(item[0], item[1], item[2], item[3]),
    )
    _assert(ranked[0][0] is has_work, "twelve wallets to send beats none")


def test_newest_project_leads_within_a_group() -> None:
    """`order` counts upward as projects are added, so it sorts descending.

    Ascending — which it was — put the oldest arrival at the top of the list
    every single time, which is the opposite of what a user opening the app
    wants to see.
    """
    projects = sorted(list_projects(), key=lambda p: int(getattr(p, "order", 0) or 0))
    if len(projects) < 2:
        return
    oldest, newest = projects[0], projects[-1]
    ranked = sorted(
        [(oldest, 0, "open", 5), (newest, 0, "open", 5)],
        key=lambda item: tgbot.project_sort_key(item[0], item[1], item[2], item[3]),
    )
    _assert(ranked[0][0] is newest, f"newest first, got {ranked[0][0].id}")


def test_project_screen_with_data() -> None:
    _set_proxies(["http://u:p@1.2.3.4:8080"])  # Start button needs a proxy pool
    uid = 10004
    _seed_user(uid, n_accounts=24)
    proj = get_project("example")
    _assert(proj is not None)
    txt = tgbot.project_ready_text(proj, 0, uid, lang="ru")
    _assert("Отправится" in txt and "24" in txt)
    _assert("Доступно слотов" in txt)
    _assert("Нужно для регистрации" in txt)
    _assert("Адрес кошелька" in txt)
    # Keyboard: start (human), links, change data, back-to-projects
    kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
    first = _first_btn_text(kb)
    _assert(first.startswith("▶ Запустить"), first)
    # Twitter/site URL buttons + «To projects» present
    cbs = [b.callback_data for row in kb.keyboard for b in row if b.callback_data]
    _assert("projects:back" in cbs)
    _assert("data:menu" in cbs)  # change accounts
    # Start callback unchanged (queue:start:<id>)
    _assert(_btn_callback(kb, "queue:start:") == "queue:start:" + proj.id)


def test_project_screen_no_data_offers_upload() -> None:
    uid = 10005
    _seed_user(uid, n_accounts=0)
    proj = get_project("example")
    txt = tgbot.project_ready_text(proj, 0, uid, lang="ru")
    _assert("Сначала загрузите аккаунты" in txt)
    kb = tgbot.project_screen_keyboard(proj, lang="ru", user_id=uid)
    _assert(_first_btn_text(kb) == "📤 Загрузить аккаунты")
    _assert(_btn_callback(kb, "data:menu") == "data:menu")
    # No start button when there is nothing to send.
    _assert(_btn_callback(kb, "queue:start:") is None)


def test_project_mint_info_line_and_button() -> None:
    """`mint_info` reaches the screen escaped; `mint_url` gets its own button."""
    from dataclasses import replace

    uid = 10006
    _seed_user(uid, n_accounts=2)
    base = get_project("example")
    _assert(base is not None)

    proj = replace(
        base,
        mint_info="18 авг · 13:00 UTC · 444 шт <тест>",
        mint_info_en="18 Aug · 13:00 UTC · 444 pcs",
        mint_url="https://example.com/mint",
    )
    ru = tgbot.project_ready_text(proj, 0, uid, lang="ru")
    _assert("🗓 Минт: 18 авг · 13:00 UTC · 444 шт &lt;тест&gt;" in ru, ru)
    _assert("<тест>" not in ru, "mint_info must be HTML-escaped")
    en = tgbot.project_ready_text(proj, 0, uid, lang="en")
    _assert("🗓 Mint: 18 Aug · 13:00 UTC · 444 pcs" in en, en)

    urls = [b.url for row in tgbot.project_screen_keyboard(
        proj, lang="ru", user_id=uid).keyboard for b in row if b.url]
    _assert("https://example.com/mint" in urls, str(urls))


def test_project_mint_info_falls_back_and_stays_optional() -> None:
    """EN falls back to the RU text; a project without mint data shows no line."""
    from dataclasses import replace

    uid = 10007
    _seed_user(uid, n_accounts=2)
    base = get_project("example")

    ru_only = replace(base, mint_info="17 авг · 3333 шт", mint_info_en="", mint_url="")
    _assert("🗓 Mint: 17 авг · 3333 шт" in
            tgbot.project_ready_text(ru_only, 0, uid, lang="en"))

    # A mint_url equal to the site link would only duplicate the site button.
    same = replace(base, mint_info="17 авг", mint_url=base.link)
    urls = [b.url for row in tgbot.project_screen_keyboard(
        same, lang="ru", user_id=uid).keyboard for b in row if b.url]
    _assert(urls.count(base.link) == 1, str(urls))

    none = replace(base, mint_info="", mint_info_en="", mint_url="")
    _assert("🗓" not in tgbot.project_ready_text(none, 0, uid, lang="ru"))


def test_queue_status_names_not_ids() -> None:
    j = sq.QueueJob(
        id=42, user_id=1, project_id="example", chat_id=1, message_id=None,
        status="running", total=24, done=14, ok=11, already=2, bad=1, fail=0,
    )
    ru = sq.format_job_status(j, lang="ru")
    # Project NAME appears; raw project_id does NOT.
    _assert("Example (demo)" in ru)
    _assert("Example (demo)" in ru)
    # Human status terms, not OK/already/bad shorthand
    _assert("Успешно" in ru and "11" in ru)
    _assert("Уже зарегистрированы" in ru)
    _assert("Отклонены" in ru)
    # Progress bar + count
    _assert("██████░░░░" in ru)
    _assert("14 из 24" in ru)
    # Technical «job»/«×N поток» gone from user view
    _assert("×" not in ru)
    _assert("job #" not in ru)
    # English parity
    en = sq.format_job_status(j, lang="en")
    _assert("Example (demo)" in en and "14 of 24" in en)
    _assert("Successful" in en and "Rejected" in en)


def test_queue_done_and_cancelled_titles() -> None:
    done = sq.QueueJob(id=43, user_id=1, project_id="example", chat_id=1,
                       message_id=None, status="done", total=24, done=24,
                       ok=18, already=3, bad=2, fail=1, skipped_dup=2)
    ru = sq.format_job_status(done, lang="ru")
    _assert("Регистрация завершена" in ru)
    _assert("Example (demo)" in ru)
    _assert("Технические ошибки" in ru)
    _assert("Пропущено" in ru)

    canc = sq.QueueJob(id=44, user_id=1, project_id="example", chat_id=1,
                       message_id=None, status="cancelled", total=10, done=4, ok=3)
    cru = sq.format_job_status(canc, lang="ru")
    _assert("Остановлено" in cru)
    _assert("Example (demo)" in cru)


def test_two_step_cancel_callbacks() -> None:
    j = sq.QueueJob(id=7, user_id=1, project_id="democ", chat_id=1,
                    message_id=None, status="running", total=3, done=1, ok=1)
    kb = sq.progress_keyboard(j, lang="ru")
    _assert(kb is not None)
    cb = kb.keyboard[0][0].callback_data
    _assert(cb == "qcancel:ask:7", cb)  # ask step, not immediate cancel
    ck = sq.cancel_confirm_keyboard(j, lang="ru")
    cbs = [b.callback_data for row in ck.keyboard for b in row]
    _assert("qcancel:yes:7" in cbs, cbs)
    # «no» must carry the job id so it refreshes the right job (P2.4).
    _assert("qcancel:no:7" in cbs, cbs)
    _assert("qcancel:no" not in cbs, cbs)
    # No callback should collide with the legacy «queue:cancel» prefix.
    _assert(not any(c.startswith("queue:cancel") for c in [cb] + cbs))


def test_is_known_user_first_visit() -> None:
    bot_db.init_db()
    sq.init_schema()
    uid = 987654
    _assert(bot_db.is_known_user(uid) is False)
    bot_db.ensure_user(uid, "x", "X")
    _assert(bot_db.is_known_user(uid) is True)


def test_projects_summary_uses_names() -> None:
    uid = 10006
    _seed_user(uid, n_accounts=2)
    # No active jobs → empty summary
    _assert(sq.format_user_jobs_summary(uid, lang="ru") == "")


def test_admin_health_navigation() -> None:
    home = tgbot.admin_keyboard_home()
    callbacks = [b.callback_data for row in home.keyboard for b in row]
    _assert("admin:health" in callbacks)
    health = tgbot.admin_keyboard_health()
    health_callbacks = [b.callback_data for row in health.keyboard for b in row]
    _assert("admin:health_refresh" in health_callbacks)
    _assert("admin:home" in health_callbacks)


class _ApiError(Exception):
    """Stand-in for telebot's ApiTelegramException (matched by .description)."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class _RecordingBot:
    """Minimal stand-in that records what would reach Telegram."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 900
        self.edit_error: Exception | None = None

    def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", {"chat_id": chat_id, "text": text, **kw}))
        self.next_id += 1
        from types import SimpleNamespace

        return SimpleNamespace(message_id=self.next_id)

    def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", {"chat_id": chat_id, "message_id": message_id, "text": text}))
        if self.edit_error is not None:
            err, self.edit_error = self.edit_error, None
            raise err

    def answer_callback_query(self, *a, **kw):
        self.calls.append(("answer", {}))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def _with_job(job, fn):
    """Register a job in the queue's in-memory state for one assertion."""
    saved = dict(sq._jobs)
    try:
        sq._jobs[job.id] = job
        return fn()
    finally:
        sq._jobs.clear()
        sq._jobs.update(saved)


def test_queue_progress_draws_while_the_user_browses_elsewhere() -> None:
    from bot import screen

    tgbot.register_screens()
    uid = 10031
    _seed_user(uid)
    fake = _RecordingBot()
    job = sq.QueueJob(
        id=77, user_id=uid, project_id="democ", chat_id=uid, message_id=5000,
        status="running", total=10, done=3, ok=3, started_at="2026-08-12T10:00:00+00:00",
    )

    # The user walked off to the proxy screen: that is the anchor now.
    screen.show(fake, uid, uid, screen.STATE_PROXY)
    anchor_before = screen.anchor(uid)
    fake.calls.clear()

    _with_job(job, lambda: sq.update_job_message(fake, job, lang="ru"))

    _assert(fake.kinds() == ["edit"], f"progress still redraws: {fake.kinds()}")
    _assert(fake.calls[0][1]["message_id"] == 5000, "…on the dashboard message")
    _assert(screen.anchor(uid) == anchor_before, "and never touches the anchor")
    _assert(screen.owns_anchor(uid, screen.STATE_PROXY), "user keeps their screen")


def test_queue_dashboard_is_reposted_when_deleted() -> None:
    uid = 10032
    _seed_user(uid)
    fake = _RecordingBot()
    fake.edit_error = _ApiError("Bad Request: message to edit not found")
    job = sq.QueueJob(
        id=78, user_id=uid, project_id="democ", chat_id=uid, message_id=5001,
        status="running", total=10, done=4, ok=4,
    )
    _with_job(job, lambda: sq.update_job_message(fake, job, lang="ru"))
    _assert(fake.kinds() == ["edit", "send"], f"edit then repost: {fake.kinds()}")
    _assert(job.message_id != 5001, "the chain remembers the new dashboard")


def test_queue_transient_edit_error_is_left_for_the_next_tick() -> None:
    uid = 10033
    _seed_user(uid)
    fake = _RecordingBot()
    fake.edit_error = _ApiError("Too Many Requests: retry after 3")
    job = sq.QueueJob(
        id=79, user_id=uid, project_id="democ", chat_id=uid, message_id=5002,
        status="running", total=10, done=5, ok=5,
    )
    _with_job(job, lambda: sq.update_job_message(fake, job, lang="ru"))
    _assert(fake.kinds() == ["edit"], f"no duplicate on a rate limit: {fake.kinds()}")
    _assert(job.message_id == 5002, "dashboard unchanged")


def test_running_job_shows_a_measured_eta() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    job = sq.QueueJob(
        id=80, user_id=10034, project_id="democ", chat_id=1, message_id=None,
        status="running", total=100, done=25, ok=25,
        started_at=(now - timedelta(minutes=10)).isoformat(),
    )
    eta = sq.job_eta_seconds(job, now=now)
    _assert(eta is not None, "rate is measurable after 10 minutes")
    # 25 rows in 10 min → 75 rows ≈ 30 min.
    _assert(1500 <= eta <= 2100, f"eta from the observed rate: {eta}")
    _assert(sq.format_eta(eta, "ru") == "30 мин", sq.format_eta(eta, "ru"))
    _assert(sq.format_eta(9000, "ru") == "2 ч 30 мин", sq.format_eta(9000, "ru"))

    text = sq.format_job_status(job, lang="ru")
    _assert("Осталось ~" in text, f"eta shown on the card: {text}")


def test_eta_is_hidden_until_it_can_be_measured() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = sq.QueueJob(
        id=81, user_id=10035, project_id="democ", chat_id=1, message_id=None,
        status="running", total=100, done=2, ok=2,
        started_at=(now - timedelta(seconds=20)).isoformat(),
    )
    _assert(sq.job_eta_seconds(fresh, now=now) is None, "20 s is not a rate")
    nothing_done = sq.QueueJob(
        id=82, user_id=10035, project_id="democ", chat_id=1, message_id=None,
        status="running", total=100, done=0,
        started_at=(now - timedelta(minutes=30)).isoformat(),
    )
    _assert(sq.job_eta_seconds(nothing_done, now=now) is None, "no rows, no estimate")
    queued = sq.QueueJob(
        id=83, user_id=10035, project_id="democ", chat_id=1, message_id=None,
        status="queued", total=100, done=0,
    )
    _assert(sq.job_eta_seconds(queued, now=now) is None, "a queued job has no rate")
    _assert("Осталось ~" not in sq.format_job_status(fresh, lang="ru"), "no fake number")


def _view_buttons(view) -> list:
    if view.markup is None:
        return []
    return [b for row in view.markup.keyboard for b in row]


def _bulk_report(uid: int, n_errors: int = 0) -> dict:
    report = {
        "project": "Hood Citizens",
        "stats": {"OK": 7, "ALREADY": 2, "BAD_RESP": 1, "FAIL": 1, "BUSY": 0, "DEDUP": 2},
        "format_bad": 3,
        "skipped_limit": 4,
        "total_ok": 7,
        "limit": 10,
        "left": 3,
        "errors": [f"• стр.{i} ошибка {i}" for i in range(1, n_errors + 1)],
    }
    tgbot.bulk_report_save(uid, report)
    return report


def test_bulk_progress_view_uses_bar_and_i18n() -> None:
    uid = 10021
    _seed_user(uid)
    view = tgbot.render_result(
        uid,
        "ru",
        {"phase": "run", "project": "Hood Citizens", "done": 14, "total": 24, "ok": 12},
    )
    _assert("██████░░░░" in view.text, f"progress bar rendered: {view.text}")
    _assert("14" in view.text and "24" in view.text, view.text)
    _assert("Hood Citizens" in view.text, "project name shown")
    # The old hardcoded RU progress line ("⏳ i/N · OK n") must be gone.
    _assert("· OK" not in view.text, f"i18n template, not the old literal: {view.text}")
    _assert(view.markup is None, "progress needs no buttons")


def test_bulk_summary_card_single_message() -> None:
    uid = 10022
    _seed_user(uid)
    _bulk_report(uid, n_errors=0)
    view = tgbot.render_result(uid, "ru", {"phase": "done"})
    callbacks = [b.callback_data for b in _view_buttons(view)]
    _assert("report:errors:0" not in callbacks, f"no errors → no button: {callbacks}")
    _assert("projects:back" in callbacks, "always a way back")
    _assert("7" in view.text and "10" in view.text, f"counters rendered: {view.text}")


def test_bulk_errors_paginate_in_place() -> None:
    uid = 10023
    _seed_user(uid)
    _bulk_report(uid, n_errors=25)

    summary = tgbot.render_result(uid, "ru", {"phase": "done"})
    cbs = [b.callback_data for b in _view_buttons(summary)]
    _assert("report:errors:0" in cbs, f"errors offered: {cbs}")
    _assert(
        any("25" in (b.text or "") for b in _view_buttons(summary)),
        "button says how many errors there are",
    )

    page0 = tgbot.render_result(uid, "ru", {"phase": "errors", "page": 0})
    lines = [l for l in page0.text.splitlines() if l.startswith("•")]
    _assert(len(lines) == tgbot.BULK_ERRORS_PER_PAGE, f"10 per page, got {len(lines)}")
    _assert("стр.1 " in page0.text, "first page starts at the first error")

    page1 = tgbot.render_result(uid, "ru", {"phase": "errors", "page": 1})
    _assert("стр.11 " in page1.text, f"second page continues: {page1.text[:80]}")

    cbs1 = [b.callback_data for b in _view_buttons(page1)]
    _assert("report:summary" in cbs1, f"way back to the card: {cbs1}")
    _assert(
        all(len((c or "").encode("utf-8")) <= 64 for c in cbs1),
        "callback_data stays under Telegram's cap",
    )

    clamped = tgbot.render_result(uid, "ru", {"phase": "errors", "page": 99})
    _assert(clamped.text, "out-of-range page still renders")


def test_bulk_report_is_per_user_and_persisted() -> None:
    uid_a, uid_b = 10024, 10025
    _seed_user(uid_a)
    _seed_user(uid_b)
    _bulk_report(uid_a, n_errors=2)
    _bulk_report(uid_b, n_errors=0)
    _assert(len(tgbot.bulk_report_load(uid_a).get("errors") or []) == 2, "read back")
    _assert(not tgbot.bulk_report_load(uid_b).get("errors"), "reports do not bleed")
    _assert(tgbot.bulk_report_load(999999) == {}, "unknown user has no report")


def main() -> None:
    tests = [
        test_progress_bar,
        test_plurals,
        test_welcome_contextual_no_data,
        test_welcome_contextual_with_data,
        test_projects_keyboard_paginates_and_hides_locked,
        test_projects_keyboard_locked_view,
        test_projects_with_work_left_come_first,
        test_a_project_with_nothing_left_sinks_below_one_with_work,
        test_newest_project_leads_within_a_group,
        test_project_screen_with_data,
        test_project_screen_no_data_offers_upload,
        test_project_mint_info_line_and_button,
        test_project_mint_info_falls_back_and_stays_optional,
        test_queue_status_names_not_ids,
        test_queue_done_and_cancelled_titles,
        test_two_step_cancel_callbacks,
        test_is_known_user_first_visit,
        test_projects_summary_uses_names,
        test_admin_health_navigation,
        test_bulk_progress_view_uses_bar_and_i18n,
        test_bulk_summary_card_single_message,
        test_bulk_errors_paginate_in_place,
        test_bulk_report_is_per_user_and_persisted,
        test_queue_progress_draws_while_the_user_browses_elsewhere,
        test_queue_dashboard_is_reposted_when_deleted,
        test_queue_transient_edit_error_is_left_for_the_next_tick,
        test_running_job_shows_a_measured_eta,
        test_eta_is_hidden_until_it_can_be_measured,
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
