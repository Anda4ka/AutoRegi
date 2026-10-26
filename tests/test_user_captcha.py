"""Per-user captcha solver keys: storage, masking, gate opening, own-key-only solves."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="user_captcha_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "test:token"
os.environ["AUTOREG_STATE_DIR"] = str(_TMP / "state")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import captcha  # noqa: E402
from bot import config  # noqa: E402
from bot import limits as bot_limits  # noqa: E402
from bot import user_captcha  # noqa: E402

config.DB_PATH = _TMP / "t.db"


def _assert(cond: bool, msg: str = "assert failed") -> None:
    if not cond:
        raise AssertionError(msg)


UID = 710001


def test_save_get_clear_round_trip() -> None:
    user_captcha.clear_key(UID)
    _assert(user_captcha.get_key(UID) is None, "no key initially")
    ok, err = user_captcha.save_key(UID, "Anti-Captcha", "abc123def456")
    _assert(ok, err)
    got = user_captcha.get_key(UID)
    _assert(got == ("anticaptcha", "abc123def456"), got)
    path = user_captcha.user_captcha_path(UID)
    _assert(path.is_file() and path.stat().st_mode & 0o777 == 0o600, "0600")
    user_captcha.clear_key(UID)
    _assert(user_captcha.get_key(UID) is None, "cleared")


def test_provider_aliases_and_bad_shapes() -> None:
    _assert(user_captcha.normalize_provider("2Captcha") == "2captcha")
    _assert(user_captcha.normalize_provider("anti-captcha.com") == "anticaptcha")
    _assert(user_captcha.normalize_provider("nope") == "")
    ok, _err = user_captcha.save_key(UID, "capsolver", "short")
    _assert(not ok, "too-short key rejected")
    ok, _err = user_captcha.save_key(UID, "nope", "abc123def456")
    _assert(not ok, "unknown provider rejected")


def test_corrupt_file_reads_as_no_key() -> None:
    path = user_captcha.user_captcha_path(UID)
    path.write_text("{not json", encoding="utf-8")
    _assert(user_captcha.get_key(UID) is None, "corrupt file is not a key (16.10)")


def test_masked_never_shows_the_whole_key() -> None:
    user_captcha.save_key(UID, "capsolver", "CAP0123456789ABCD")
    shown = user_captcha.masked(UID)
    _assert(shown.startswith("capsolver: …"), shown)
    _assert("CAP0123456789ABCD" not in shown, "full key never shown")
    user_captcha.clear_key(UID)


class _P:
    """A requires_captcha project, minimal shape for the gate."""

    id = "capproj"
    name = "Cap Project"
    requires_captcha = True
    only_user_ids: tuple = ()
    min_ok_limit = 0


def test_own_key_opens_captcha_project() -> None:
    # Init through the same db module object the limits gate actually uses —
    # import-order-sensitive suites may have re-imported bot.* with another env.
    bot_limits.bot_db.init_db()
    uid = 710002
    bot_limits.bot_db.ensure_user(uid, "k", "K")
    _assert(not bot_limits.project_allowed(_P, uid), "no key and not VIP → closed")
    user_captcha.save_key(uid, "2captcha", "abc123def4567890")
    _assert(bot_limits.project_allowed(_P, uid), "own key opens it at any tier")
    user_captcha.clear_key(uid)
    _assert(not bot_limits.project_allowed(_P, uid), "cleared key closes it again")


def test_solve_turnstile_uses_own_key_only() -> None:
    """Own key replaces the operator's accounts; no fallback when it dies."""
    uid = 710003
    calls: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def _fake_post(url, json=None, timeout=None, **_kw):  # noqa: A002
        calls.append((url, dict(json or {})))
        if url.endswith("/getTaskResult"):
            return _Resp({"errorId": 0, "status": "ready",
                          "solution": {"token": "own-token"}})
        return _Resp({"errorId": 0, "taskId": "1"})

    user_captcha.save_key(uid, "2captcha", "USERKEY12345678")
    captcha.reset_dead_providers()
    with mock.patch.object(captcha.requests, "post", _fake_post), \
         mock.patch.object(captcha.time, "sleep", lambda _s: None):
        token, detail = captcha.solve_turnstile(
            "https://x.test", "0xsitekey", user_id=uid
        )
    _assert(token == "own-token", detail)
    used = [c for c in calls if c[0].endswith("/createTask")][0]
    _assert(used[1]["clientKey"] == "USERKEY12345678", "own key on the wire")
    _assert(all("anti-captcha" not in c[0] and "capsolver" not in c[0] for c in calls),
            "operator accounts never asked")
    user_captcha.clear_key(uid)


def test_dead_own_key_does_not_poison_operator_account() -> None:
    """The dead-account memo is keyed by provider + key tail."""
    uid = 710004
    user_captcha.save_key(uid, "2captcha", "USERKEYAAAAAAAA99")

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def _no_balance(url, json=None, timeout=None, **_kw):  # noqa: A002
        return _Resp({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE",
                      "errorDescription": "no balance"})

    captcha.reset_dead_providers()
    with mock.patch.object(captcha.requests, "post", _no_balance):
        _tok, detail = captcha.solve_turnstile("https://x.test", "k", user_id=uid)
    _assert("no balance" in detail, detail)
    # The operator's 2captcha account with a different key tail is untouched.
    _assert(captcha._dead_reason("2captcha:OPER") == "", "other account usable")
    _assert("no balance" in captcha._dead_reason("2captcha:AA99"), "own account dead")
    captcha.reset_dead_providers()
    user_captcha.clear_key(uid)


def test_anticaptcha_task_shape() -> None:
    task = captcha._build_task(
        next(p for p in captcha.PROVIDERS if p.name == "anticaptcha"),
        "https://x.test", "key", "", "the-action", "cd1",
    )
    _assert(task["type"] == "TurnstileTaskProxyless", task)
    _assert(task["action"] == "the-action", task)
    _assert(task["cData"] == "cd1", "anti-captcha keeps its own casing")


def test_multiple_keys_chain_and_upsert() -> None:
    """One key per provider, up to 3; save replaces the same provider's key."""
    uid = 710005
    user_captcha.clear_key(uid)
    ok, err = user_captcha.save_key(uid, "capsolver", "CAP-AAAAAAAA99")
    _assert(ok, err)
    ok, err = user_captcha.save_key(uid, "2captcha", "TWOAAAAAAAA99")
    _assert(ok, err)
    ok, err = user_captcha.save_key(uid, "anticaptcha", "ANTIAAAAAAAA99")
    _assert(ok, err)
    ok, err = user_captcha.save_key(uid, "capsolver", "CAP-BBBBBBBB99")
    _assert(ok, err)
    rows = user_captcha.get_keys(uid)
    _assert([p for p, _k in rows] == ["capsolver", "2captcha", "anticaptcha"], rows)
    _assert(rows[0] == ("capsolver", "CAP-BBBBBBBB99"), f"upsert replaced: {rows}")
    user_captcha.clear_key(uid)


def test_multi_key_cap_of_three() -> None:
    uid = 710006
    user_captcha.clear_key(uid)
    for prov, key in (
        ("capsolver", "CAP-AAAAAAAA99"),
        ("2captcha", "TWOAAAAAAAA99"),
        ("anticaptcha", "ANTIAAAAAAAA99"),
    ):
        ok, err = user_captcha.save_key(uid, prov, key)
        _assert(ok, err)
    # A fourth would need a fourth provider — impossible with one-per-provider,
    # so the cap is structural; removing one frees the slot again.
    _assert(len(user_captcha.get_keys(uid)) == 3, "three stored")
    _assert(user_captcha.remove_key(uid, "2captcha"), "removed one")
    _assert([p for p, _k in user_captcha.get_keys(uid)] == ["capsolver", "anticaptcha"],
            user_captcha.get_keys(uid))
    ok, err = user_captcha.save_key(uid, "2captcha", "TWOCCCCCCCC99")
    _assert(ok, err)
    user_captcha.clear_key(uid)


def test_legacy_single_key_file_reads_as_one_row_chain() -> None:
    uid = 710007
    user_captcha.clear_key(uid)
    path = user_captcha.user_captcha_path(uid)
    path.write_text(
        json.dumps({"provider": "2captcha", "key": "OLDKEYAAAA99"}),
        encoding="utf-8",
    )
    _assert(user_captcha.get_keys(uid) == [("2captcha", "OLDKEYAAAA99")],
            user_captcha.get_keys(uid))
    user_captcha.clear_key(uid)


def test_solve_walks_own_key_chain_when_first_is_dead() -> None:
    """Two own keys: the dead first is skipped and the second solves the row."""
    uid = 710008
    user_captcha.clear_key(uid)
    user_captcha.save_key(uid, "capsolver", "OWNCAPAAAA99")
    user_captcha.save_key(uid, "2captcha", "OWNTWOAAAA99")

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def _fake_post(url, json=None, timeout=None, **_kw):  # noqa: A002
        client = (json or {}).get("clientKey") or ""
        if url.endswith("/createTask"):
            if client == "OWNCAPAAAA99":
                return _Resp({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE",
                              "errorDescription": "no balance"})
            return _Resp({"errorId": 0, "taskId": "2"})
        if url.endswith("/getTaskResult"):
            return _Resp({"errorId": 0, "status": "ready",
                          "solution": {"token": "second-key-token"}})
        return _Resp({"errorId": 0, "balance": 1.0})

    captcha.reset_dead_providers()
    with mock.patch.object(captcha.requests, "post", _fake_post), \
         mock.patch.object(captcha.time, "sleep", lambda _s: None):
        token, detail = captcha.solve_turnstile(
            "https://x.test", "0xsitekey", user_id=uid
        )
    _assert(token == "second-key-token", detail)
    captcha.reset_dead_providers()
    user_captcha.clear_key(uid)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
