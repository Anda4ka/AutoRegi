"""Form-closed detection + runtime project disable."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolated DB for this process
_td = tempfile.mkdtemp(prefix="form_closed_")
os.environ["BOT_DB"] = str(Path(_td) / "t.db")
os.environ["BOT_RESULTS"] = str(Path(_td) / "r.jsonl")

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import form_closed as fc  # noqa: E402
from bot.registry import get_project, list_projects, load_projects  # noqa: E402


def _ok(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_is_form_closed_phrases() -> None:
    _ok(fc.is_form_closed_detail("gate closed", "BAD_RESP"), "gate closed")
    _ok(
        fc.is_form_closed_detail(
            '{"ok":false,"error":"gate closed"}', "BAD_RESP"
        ),
        "json gate closed",
    )
    _ok(
        fc.is_form_closed_detail("Applications are closed right now.", "BAD_RESP"),
        "applications closed",
    )
    _ok(fc.is_form_closed_detail("регистрация закрыта", "FAIL"), "ru closed")
    _ok(
        fc.is_form_closed_detail("Whitelist closed for now", "BAD_RESP"),
        "wl closed",
    )
    _ok(
        fc.is_form_closed_detail("HTTP 404: Not Found", "BAD_RESP"),
        "404 endpoint",
    )
    _ok(fc.is_form_closed_detail("status=410 gone", "FAIL"), "410 endpoint")


def test_is_form_closed_rejects_already_and_ok() -> None:
    _ok(
        not fc.is_form_closed_detail("This wallet has already been submitted.", "BAD_RESP"),
        "already submitted",
    )
    _ok(
        not fc.is_form_closed_detail('{"error":"wallet taken"}', "BAD_RESP"),
        "wallet taken",
    )
    _ok(not fc.is_form_closed_detail("gate closed", "OK"), "OK never closed")
    _ok(not fc.is_form_closed_detail("gate closed", "ALREADY"), "ALREADY never")
    _ok(
        not fc.is_form_closed_detail("invalid wallet address", "BAD_RESP"),
        "invalid wallet alone",
    )
    _ok(not fc.is_form_closed_detail("proxy timeout", "FAIL"), "proxy fail")
    # A block page carries the endpoint's own HTTP code (script.google.com
    # answered 404 via a dead exit on 2026-09-16) and must not page admins
    # about a working project.
    _ok(
        not fc.is_form_closed_detail(
            "HTTP 404: proxy block page (ppConfig)", "FAIL"
        ),
        "proxy block page is not a closed form",
    )
    _ok(
        not fc.is_form_closed_detail(
            "HTTP 404: <!DOCTYPE html><html lang=\"en\"><head>"
            "<script nonce=\"x\">window['ppConfig'] = {productName: "
            "'26981ed0d57bbad37e728ff58134270c'",
            "BAD_RESP",
        ),
        "raw ppConfig HTML 404 is not a closed form",
    )
    _ok(
        not fc.is_form_closed_detail(
            "proxy refused connection relay-1.example.net:8080", "FAIL"
        ),
        "proxy transport is not a closed form",
    )
    _ok(
        not fc.is_form_closed_detail("X handle not found", "BAD_RESP"),
        "user not found",
    )


def test_missing_gas_entrypoint_counts_as_closed() -> None:
    """A deployment whose script version defines no doPost writes nothing.

    A live adapter 200'd its localized "script function not found" page for 1013
    rows on 2026-08-14 and, matching no marker here, never alerted.
    """
    from bot.http_client import gas_missing_entrypoint, parse_gas_html_error

    page = (
        '<!doctype html><html><head><title>Lỗi</title></head><body>'
        '<div style="text-align:center;font-family:monospace;margin:50px auto 0">'
        "Không tìm thấy hàm tập lệnh: doPost</div></body></html>"
    )
    _ok(gas_missing_entrypoint(page) == "doPost", "entry point named in any locale")
    _ok(
        parse_gas_html_error(page) == "Không tìm thấy hàm tập lệnh: doPost",
        "the page's own line beats a generic shrug",
    )
    _ok(
        fc.is_form_closed_detail(
            "GAS script function not found: doPost — the deployment no longer "
            "accepts submissions",
            "BAD_RESP",
        ),
        "missing entry point alerts",
    )
    # A script that merely threw is a transient failure, not a closed form.
    thrown = (
        '<div style="font-family:monospace">Exception: Service Spreadsheets '
        'failed while accessing document (line 12, file "Code")</div>'
    )
    _ok(gas_missing_entrypoint(thrown) == "", "a thrown script is not a gone one")
    _ok(
        not fc.is_form_closed_detail(parse_gas_html_error(thrown), "FAIL"),
        "thrown script never closes a project",
    )


def test_google_closed_form_page_is_detected() -> None:
    """A project's form closed on 2026-08-12 and nothing alerted: the adapter
    reported the token miss as "form token unavailable: HTTP 200", which is
    indistinguishable from a proxy interstitial and matches nothing here."""
    from bot.http_client import GOOGLE_FORM_CLOSED_DETAIL

    _ok(
        fc.is_form_closed_detail(GOOGLE_FORM_CLOSED_DETAIL, "BAD_RESP"),
        "the detail the Forms adapters now emit must alert",
    )
    _ok(
        fc.is_form_closed_detail(
            "https://docs.google.com/forms/d/e/ABC/closedform?hl=en", "FAIL"
        ),
        "the closedform URL alone is a definite signal",
    )
    # Language-proof: a German close page carries no English prose at all.
    _ok(
        fc.is_form_closed_detail(
            "landed on .../closedform — Das Formular nimmt keine Antworten mehr an",
            "BAD_RESP",
        ),
        "localized close page still detected via the URL",
    )
    # …and the ambiguous wording must still NOT disable a project on its own:
    # a proxy interstitial produces exactly this and the form may be healthy.
    _ok(
        not fc.is_form_closed_detail("form token unavailable: HTTP 200", "FAIL"),
        "ambiguous token miss must not trip the alert by itself",
    )


class _ClosedFormResponse:
    """What requests returns after following the 302 to /closedform."""

    status_code = 200
    url = "https://docs.google.com/forms/d/e/ABC/closedform?hl=en&embedded=true"
    text = (
        "<!DOCTYPE html><html><body>The form <b>Demo</b> is no longer "
        "accepting responses.</body></html>"
    )


class _ClosedFormSession:
    gets = 0

    def __init__(self) -> None:
        self.headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, *_a, **_kw):
        _ClosedFormSession.gets += 1
        return _ClosedFormResponse()

    def post(self, *_a, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("posted to a closed form")


def test_forms_adapters_report_closed_instead_of_retrying() -> None:
    """Closed form → BAD_RESP on the first GET, no retry storm, and a detail
    that is_form_closed_detail actually recognizes."""
    import importlib

    for mod_name, entry, extra in (
    ):
        mod = importlib.import_module(mod_name)
        real_session = mod.requests.Session
        _ClosedFormSession.gets = 0
        mod.requests.Session = _ClosedFormSession
        try:
            result = mod.submit(entry, ["http://user:pw@proxy.test:8080"], 0,
                                retries=4, retry_delay=0.01, **extra)
        finally:
            mod.requests.Session = real_session
        _ok(result["status"] == "BAD_RESP", f"{mod_name}: {result}")
        _ok(
            fc.is_form_closed_detail(result["detail"], result["status"]),
            f"{mod_name}: detail {result['detail']!r} does not alert",
        )
        _ok(
            _ClosedFormSession.gets == 1,
            f"{mod_name}: retried a closed form {_ClosedFormSession.gets}x",
        )


def test_runtime_disable_roundtrip() -> None:
    bot_db.init_db()
    pid = "democ"
    _ok(not bot_db.is_project_runtime_disabled(pid), "start enabled")
    bot_db.set_project_runtime_disabled(pid, True, admin_id=1)
    _ok(bot_db.is_project_runtime_disabled(pid), "disabled")
    _ok(pid in bot_db.list_runtime_disabled_project_ids(), "listed")
    bot_db.set_project_runtime_disabled(pid, False, admin_id=1)
    _ok(not bot_db.is_project_runtime_disabled(pid), "re-enabled")
    _ok(pid not in bot_db.list_runtime_disabled_project_ids(), "unlisted")


def test_registry_hides_runtime_disabled() -> None:
    bot_db.init_db()
    load_projects(force=True)
    # Pick any loaded project if present
    live = list_projects()
    if not live:
        print("skip registry hide: no projects loaded")
        return
    pid = live[0].id
    bot_db.set_project_runtime_disabled(pid, True, admin_id=1)
    try:
        _ok(get_project(pid) is None, "get_project hides RT-off")
        _ok(
            get_project(pid, allow_runtime_disabled=True) is not None,
            "allow_runtime_disabled sees it",
        )
        _ok(all(p.id != pid for p in list_projects()), "list_projects hides")
        _ok(
            any(p.id == pid for p in list_projects(include_runtime_disabled=True)),
            "include flag shows",
        )
    finally:
        bot_db.set_project_runtime_disabled(pid, False, admin_id=1)


def test_alert_dedup_marker() -> None:
    bot_db.init_db()
    pid = "democ"
    _ok(fc.should_send_form_closed_alert(pid), "first ok")
    fc.mark_form_closed_alert_sent(pid)
    _ok(not fc.should_send_form_closed_alert(pid), "dedup blocks")


def test_alert_delivery_and_markup() -> None:
    bot_db.init_db()
    pid = "alert_delivery"
    old_admins = config.ADMIN_IDS
    sent: list[tuple[int, str, object]] = []
    config.ADMIN_IDS = [111, 222]
    try:
        ok = fc.maybe_alert_form_closed(
            project_id=pid,
            project_name="Demo",
            status="BAD_RESP",
            detail="HTTP 404: Not Found",
            send=lambda aid, text, markup: sent.append((aid, text, markup)),
        )
        _ok(ok, "alert sent")
        _ok([x[0] for x in sent] == [111, 222], "all admins")
        _ok("перестал принимать заявки" in sent[0][1], "clear alert title")
        callbacks = [
            b.callback_data
            for row in sent[0][2].keyboard
            for b in row
        ]
        _ok(f"admin:proj_off:{pid}" in callbacks, "disable callback")
        _ok(
            not fc.should_send_form_closed_alert(pid),
            "successful send marks dedup",
        )
    finally:
        config.ADMIN_IDS = old_admins


def test_failed_delivery_does_not_mark_dedup() -> None:
    bot_db.init_db()
    pid = "alert_delivery_failure"
    old_admins = config.ADMIN_IDS
    config.ADMIN_IDS = [333]

    def _fail(_aid, _text, _markup):
        raise RuntimeError("telegram unavailable")

    try:
        ok = fc.maybe_alert_form_closed(
            project_id=pid,
            project_name="Demo",
            status="BAD_RESP",
            detail="gate closed",
            send=_fail,
        )
        _ok(not ok, "failed send")
        _ok(fc.should_send_form_closed_alert(pid), "failed send remains retryable")
    finally:
        config.ADMIN_IDS = old_admins


def test_a_sold_out_supply_is_a_closed_form() -> None:
    """A project that ran out never says "closed" — and used to slip through.

    robidrop answered `409 {"error":"Series is full - every pass has been
    claimed."}` to 1876 rows across a full day and raised no alert at all,
    because none of the closed phrases appear in that sentence; hoodroost only
    alerted on the first half of "Applications are closed. The roost is full."
    Both were switched off by hand on 2026-08-16 after zero OK rows followed
    the first refusal.
    """
    for detail in (
        'HTTP 409: {"error":"Series is full - every pass has been claimed."}',
        'HTTP 403: {"error":"Applications are closed. The roost is full."}',
        '{"error":"sold out"}',
        '{"error":"No spots left"}',
        '{"error":"The collection is fully claimed"}',
        '{"error":"All passes have been claimed"}',
    ):
        _ok(fc.is_form_closed_detail(detail, "BAD_RESP"), f"missed: {detail}")

    # A bare "full" belongs to plumbing, and a duplicate is not a closure.
    for detail in (
        '{"error":"wallet taken"}',
        '{"error":"This wallet has already applied. One entry each."}',
        '{"error":"rate_limited"}',
        '{"error":"invalid_email"}',
        "duplicate key value violates unique constraint",
        "proxy connect timeout relay-eu.proxyshard.com:8080",
    ):
        _ok(not fc.is_form_closed_detail(detail, "BAD_RESP"), f"false positive: {detail}")

    # OK / ALREADY are never a closure whatever the body says.
    _ok(not fc.is_form_closed_detail('{"error":"sold out"}', "ALREADY"), "ALREADY is not closed")


if __name__ == "__main__":
    test_is_form_closed_phrases()
    test_is_form_closed_rejects_already_and_ok()
    test_a_sold_out_supply_is_a_closed_form()
    test_google_closed_form_page_is_detected()
    test_forms_adapters_report_closed_instead_of_retrying()
    test_runtime_disable_roundtrip()
    test_registry_hides_runtime_disabled()
    test_alert_dedup_marker()
    test_alert_delivery_and_markup()
    test_failed_delivery_does_not_mark_dedup()
    print("test_form_closed: OK")
